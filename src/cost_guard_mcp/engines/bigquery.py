import concurrent.futures
import logging
import re

from google.cloud import bigquery, bigquery_reservation_v1

from cost_guard_mcp.errors import SanitizedEngineError, redact_secrets, sanitize_exceptions
from cost_guard_mcp.pricing.bigquery_pricing import ON_DEMAND_USD_PER_TIB, TIB_IN_BYTES
from cost_guard_mcp.types import AccuracyTier, CostEstimate, CredentialCheckResult

logger = logging.getLogger(__name__)

# GCP project ID format: lowercase letter, then lowercase letters/digits/hyphens, 6-30 chars
# total, cannot end with a hyphen. `project` is not currently reachable from an MCP tool
# parameter, but it does flow into a hand-built API filter string below — validate before
# interpolating, same defense-in-depth reasoning as the Snowflake warehouse-name validator.
_GCP_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9\-]{4,28}[a-z0-9]$")


def _validate_project_id(project: str) -> str:
    if not _GCP_PROJECT_ID_RE.fullmatch(project):
        raise ValueError(f"Invalid GCP project ID: {project!r}")
    return project


@sanitize_exceptions("bigquery")
def is_capacity_billed(project: str, location: str = "US") -> bool:
    """Return True if `project` has a BigQuery Reservation assignment (Editions/capacity billing).

    LIMITATION: this checks only the given location (default "US" multi-region). A project
    could have assignments in other locations not covered by a single check — acceptable for
    v1 since most Sandbox/trial usage is US multi-region; revisit if this causes a
    false-negative (silently applying on-demand pricing logic to a capacity-billed query
    running against a differently-located reservation).
    """
    project = _validate_project_id(project)
    client = bigquery_reservation_v1.ReservationServiceClient()
    parent = f"projects/{project}/locations/{location}"
    # `query` is not optional in practice: proto3 can't distinguish "omitted" from "empty
    # string" on the wire, and the live API rejects an empty query with a 400 asking for
    # this exact `assignee=` filter format. Verified against a real project (2026-09-13) —
    # confirms the gap this project's own plan flagged as an unverified spike.
    assignments = client.search_all_assignments(
        request={"parent": parent, "query": f"assignee=projects/{project}"}
    )
    return any(assignments)


_BQ_ACCURACY_TO_TIER = {
    "PRECISE": AccuracyTier.PRECISE,
    "LOWER_BOUND": AccuracyTier.UPPER_BOUND,
    "UPPER_BOUND": AccuracyTier.UPPER_BOUND,
    "UNKNOWN": AccuracyTier.UPPER_BOUND,
}


def _references_remote_billing(sql: str) -> bool:
    """Conservative textual heuristic for queries that likely trigger BigQuery remote
    functions or BigQuery ML remote-model inference (`ML.GENERATE_TEXT`), both of which
    incur separate Cloud Run/Vertex AI billing that a byte-based dollar estimate cannot
    see.

    Deliberately unconditional and text-based rather than derived from the dry-run
    response's `statistics.query.referencedRoutines` field (researched 2026-09-18):
    that field is real and documented (BigQuery's own v2 Discovery Document -
    googleapis.com/discovery/v1/apis/bigquery/v2/rest - `JobStatistics2.referencedRoutines`,
    an array of `{projectId, datasetId, routineId}`), but no Google source states whether
    it populates on a *dry run* specifically, only that `referencedTables` (its sibling
    field, already read above for the RLS caveat) does. More importantly,
    `referencedRoutines` can NEVER detect `ML.GENERATE_TEXT` at all - BQML remote-model
    inference references a `ModelReference`, and `JobStatistics2` has no
    `referencedModels`-equivalent field - so a structured read could only ever supplement,
    never replace, this text check for the remote-function half. Next concrete step for a
    maintainer with real BigQuery credentials: run a dry-run query that invokes an
    existing remote function (no `CREATE FUNCTION` DDL) and inspect
    `query_job._properties["statistics"]["query"].get("referencedRoutines")` - if it's
    populated, add a supplemental read there (each returned routine still needs a
    `routines.get` call, since the reference alone carries no `remoteFunctionOptions`),
    OR'd with this text check rather than replacing it.
    """
    upper_sql = sql.upper()
    if "ML.GENERATE_TEXT" in upper_sql:
        return True
    return "CREATE FUNCTION" in upper_sql and "REMOTE" in upper_sql


@sanitize_exceptions("bigquery")
def dry_run(sql: str, project: str | None = None) -> CostEstimate:
    """Estimate BigQuery query cost via a dry run. Tagged PRECISE unless BigQuery's own
    totalBytesProcessedAccuracy says otherwise, or the project is capacity-billed."""
    client = bigquery.Client(project=project)
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    query_job = client.query(sql, job_config=job_config)

    total_bytes_processed = query_job.total_bytes_processed or 0
    raw_accuracy = (
        query_job._properties.get("statistics", {})
        .get("query", {})
        .get("totalBytesProcessedAccuracy", "UNKNOWN")
    )
    tier = _BQ_ACCURACY_TO_TIER.get(raw_accuracy, AccuracyTier.UPPER_BOUND)

    caveats: list[str] = []
    if tier != AccuracyTier.PRECISE:
        caveats.append(
            f"BigQuery reported this estimate's own accuracy as '{raw_accuracy}', not "
            "PRECISE — treating it conservatively as UPPER_BOUND."
        )

    # BigQuery's dry-run response always reports 0 bytes processed for tables protected by
    # row-level security, by design, to prevent a side-channel that would let a query's byte
    # cost reveal information about rows the caller can't see. Without this caveat, an
    # RLS-protected query would get tagged PRECISE with a $0.00 estimate and sail through any
    # cost cap in run_query_bounded (which only fail-closes on a None estimate, never a
    # suspiciously-low one) before running for real.
    if total_bytes_processed == 0 and query_job.referenced_tables:
        caveats.append(
            "BigQuery dry runs always report 0 bytes processed for tables protected by "
            "row-level security, by design, to prevent a side-channel — a $0.00 estimate "
            "here must NOT be trusted as proof this query is free to run."
        )

    if _references_remote_billing(sql):
        caveats.append(
            "This query appears to invoke a BigQuery remote function or ML.GENERATE_TEXT "
            "(BigQuery ML remote-model inference) — both incur separate Cloud Run/Vertex AI "
            "billing that this byte-based dollar estimate does not include."
        )

    try:
        capacity_billed = is_capacity_billed(client.project)
    except SanitizedEngineError:
        caveats.append(
            "Could not verify BigQuery billing model (Reservations API call failed) - "
            "returning a byte estimate only, no dollar figure, to avoid reporting a "
            "possibly-wrong-billing-model dollar amount."
        )
        return CostEstimate(
            engine="bigquery",
            accuracy_tier=tier,
            estimated_bytes=total_bytes_processed,
            estimated_cost_usd=None,
            caveats=caveats,
        )

    if capacity_billed:
        caveats.append(
            "This project is on BigQuery Editions/capacity billing (slot-hours), which has "
            "no fixed $/byte rate — no dollar estimate is possible from bytes alone."
        )
        return CostEstimate(
            engine="bigquery",
            accuracy_tier=tier,
            estimated_bytes=total_bytes_processed,
            estimated_cost_usd=None,
            caveats=caveats,
        )

    estimated_cost_usd = (total_bytes_processed / TIB_IN_BYTES) * ON_DEMAND_USD_PER_TIB
    return CostEstimate(
        engine="bigquery",
        accuracy_tier=tier,
        estimated_bytes=total_bytes_processed,
        estimated_cost_usd=round(estimated_cost_usd, 6),
        caveats=caveats,
    )


# How long to wait for a bounded query to finish before giving up and cancelling it. This
# is a Polling Timeout in google-api-core's terms — confirmed via the base
# PollingFuture._blocking_poll implementation, not just QueryJob.result()'s own (looser)
# docstring wording — so it genuinely bounds the whole wait, not just one HTTP call. Without
# this, a query stuck behind slot contention or a cold warehouse could block a tool call
# indefinitely, which defeats the point of a "bounded" execution tool.
_MAX_EXECUTION_WAIT_SECONDS = 120


@sanitize_exceptions("bigquery")
def execute_bounded(
    sql: str,
    max_bytes_billed: int | None,
    max_rows: int | None,
    project: str | None = None,
) -> tuple[list[dict], int, bool]:
    """Execute `sql` with optional byte and row bounds.

    Row bounding wraps the query in `LIMIT max_rows + 1` so the fetch itself never pulls
    more than max_rows + 1 rows over the wire — the caller can tell "there were exactly
    max_rows" apart from "there were more than max_rows" via the returned bool.
    """
    client = bigquery.Client(project=project)

    wrapped_sql = sql
    if max_rows is not None:
        inner_sql = sql.strip().rstrip(";").strip()
        # `sql` is the caller's own query, passed as this tool's actual `sql` parameter -
        # wrapping their query in a LIMIT subquery to cap rows is this function's job, not
        # untrusted input reaching a query built from a different source.
        wrapped_sql = f"SELECT * FROM ({inner_sql}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

    job_config = (
        bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
        if max_bytes_billed is not None
        else bigquery.QueryJobConfig()
    )
    query_job = client.query(wrapped_sql, job_config=job_config)
    try:
        rows = [dict(row) for row in query_job.result(timeout=_MAX_EXECUTION_WAIT_SECONDS)]
    except concurrent.futures.TimeoutError:
        try:
            query_job.cancel()
        except Exception:  # noqa: BLE001, S110 - best-effort cancel; the TimeoutError below is
            # the message that matters to the caller, a failed cancel must not mask it
            pass
        logger.warning(
            "engine=bigquery execute_bounded timed out after %ss, cancelled job_id=%s",
            _MAX_EXECUTION_WAIT_SECONDS,
            query_job.job_id,
        )
        raise TimeoutError(
            f"Query exceeded {_MAX_EXECUTION_WAIT_SECONDS}s and was cancelled "
            f"(job_id={query_job.job_id})."
        ) from None

    row_cap_hit = max_rows is not None and len(rows) > max_rows
    if row_cap_hit:
        rows = rows[:max_rows]

    return rows, len(rows), row_cap_hit


def check_credentials(project: str | None = None) -> CredentialCheckResult:
    """Verify BigQuery credentials/connectivity without running or dry-running any query.

    Deliberately does NOT use @sanitize_exceptions: a failed check is the expected, useful
    result here, not an error condition — this function always returns a
    CredentialCheckResult (never raises for a credential/connectivity failure) so a caller
    can distinguish "not configured yet" from "actually broken" before attempting a real
    estimate_query_cost/run_query_bounded call.
    """
    try:
        client = bigquery.Client(project=project)
        service_account_email = client.get_service_account_email()
    except Exception as exc:  # noqa: BLE001 - reporting failure as data, not raising, by design
        return CredentialCheckResult(engine="bigquery", ok=False, detail=redact_secrets(str(exc)))

    return CredentialCheckResult(
        engine="bigquery",
        ok=True,
        detail=(
            f"Authenticated to project '{client.project}' "
            f"(BigQuery service account: {service_account_email})."
        ),
    )
