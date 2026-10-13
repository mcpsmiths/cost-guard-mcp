import concurrent.futures
import logging
import re

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery, bigquery_reservation_v1
from google.cloud.bigquery.retry import DEFAULT_RETRY

from cost_guard_mcp.errors import (
    SanitizedEngineError,
    present_remote_text,
    redact_secrets,
    sanitize_exceptions,
)
from cost_guard_mcp.pricing.bigquery_pricing import ON_DEMAND_USD_PER_TIB, TIB_IN_BYTES
from cost_guard_mcp.sql_scope import single_statement_body
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
        request={"parent": parent, "query": f"assignee=projects/{project}"},
        timeout=_MAX_API_WAIT_SECONDS,
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
    query_job = client.query(
        sql,
        job_config=job_config,
        retry=_API_RETRY,
        timeout=_MAX_API_WAIT_SECONDS,
        job_retry=None,
    )

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


_MIB = 1024**2
# BigQuery bills every on-demand query a 10 MiB minimum (and 10 MiB per referenced table), so
# a maximumBytesBilled below this would make every uncached query fail at the provider.
_MIN_MAXIMUM_BYTES_BILLED = 10 * _MIB
_INT64_MAX = 2**63 - 1  # maximumBytesBilled is an int64 (string-encoded) API field
# The smallest dollar cap whose derived byte cap would reach the int64 ceiling.
_INT64_MAX_USD_CAP = _INT64_MAX / TIB_IN_BYTES * ON_DEMAND_USD_PER_TIB
# The errors[].reason BigQuery attaches when a job would bill more than its
# maximumBytesBilled. Matched by reason, not exception class: the API path raises
# BadRequest (400) while the pinned client maps a failed job with this reason to
# InternalServerError (500) because its reason table has no entry for it.
_BYTES_BILLED_LIMIT_EXCEEDED_REASON = "bytesBilledLimitExceeded"


def cost_cap_to_max_bytes_billed(max_estimated_cost_usd: float) -> int:
    """The server-side maximumBytesBilled equivalent of an on-demand dollar cap.

    BigQuery compares maximumBytesBilled against totalBytesBilled, which is the processed
    byte count rounded UP to the next MiB (10 MiB minimum), while the dry-run dollar figure
    comes from the unrounded count. Rounding the derived cap up the same way keeps "the gate
    admits => the provider admits" for single-table queries; without it a query estimated
    within a MiB of the cap would pass the gate and then be rejected by the provider.
    """
    # maximumBytesBilled is an int64 on the API. Compare in float space BEFORE converting,
    # so a huge finite cap neither overflows int() (>= ~1e297 USD raised OverflowError)
    # nor produces a 200-digit value the API would reject with a 400: anything at or
    # above the int64 ceiling is clamped to it, which BigQuery accepts and which simply
    # means "no effective server-side cap" (the pre-flight gate still applies).
    if max_estimated_cost_usd >= _INT64_MAX_USD_CAP:
        return _INT64_MAX
    raw_bytes = int(max_estimated_cost_usd / ON_DEMAND_USD_PER_TIB * TIB_IN_BYTES)
    rounded_up_to_mib = -(-raw_bytes // _MIB) * _MIB
    return min(max(rounded_up_to_mib, _MIN_MAXIMUM_BYTES_BILLED), _INT64_MAX)


class ProviderByteCapExceeded(SanitizedEngineError):
    """BigQuery refused to run a job because it would bill more than the maximumBytesBilled
    attached to it. Nothing was billed. A SanitizedEngineError (so it passes through
    sanitize_exceptions unchanged) with a fixed, secret-free message; run_query_bounded maps
    it to a structured refusal instead of a ToolError."""

    def __init__(self, maximum_bytes_billed: int | None) -> None:
        if maximum_bytes_billed is None:
            # Reachable when the project or organisation carries a default
            # maximumBytesBilled and this server attached none (no caps, or an infinite
            # cost cap): say so instead of asserting a cap "of None bytes".
            message = (
                "BigQuery refused to run this query because it would exceed a "
                "maximumBytesBilled limit configured outside this server (cost-guard "
                "attached no byte cap to the job). Nothing was billed."
            )
        else:
            message = (
                "BigQuery refused to run this query because it would bill more than the "
                f"server-side maximumBytesBilled limit of {maximum_bytes_billed} bytes "
                "attached to the job. Nothing was billed."
            )
        super().__init__(message)
        self.maximum_bytes_billed = maximum_bytes_billed


def _is_bytes_billed_limit_exceeded(exc: GoogleAPICallError) -> bool:
    return any(
        isinstance(error, dict) and error.get("reason") == _BYTES_BILLED_LIMIT_EXCEEDED_REASON
        for error in exc.errors
    )


# How long to wait for a bounded query to finish before giving up and cancelling it. This
# is a Polling Timeout in google-api-core's terms — confirmed via the base
# PollingFuture._blocking_poll implementation, not just QueryJob.result()'s own (looser)
# docstring wording — so it genuinely bounds the whole wait, not just one HTTP call. Without
# this, a query stuck behind slot contention or a cold warehouse could block a tool call
# indefinitely, which defeats the point of a "bounded" execution tool.
_MAX_EXECUTION_WAIT_SECONDS = 120

# Bound on every *other* BigQuery API round-trip this module makes - the service-account
# lookup, the dry-run and execution job submissions (jobs.insert), and the Reservations
# search. Without an explicit retry/timeout each of those inherits google-cloud-bigquery's
# DEFAULT_RETRY (a 600 s deadline with no per-request timeout), and client.query() wraps
# jobs.insert in a second, hard-coded 600 s job-level retry whenever job_retry is not None -
# so a read-only tool could block for ~10-20 minutes while Google is unreachable, and an
# abandoned run_query_bounded thread could still submit the billed job minutes after the
# host cancelled it. 30 s here plus the 120 s result() wait above stays inside the README's
# "tool calls can run up to 120 seconds" claim for the phase that actually bills. job_retry
# must be None (not a shorter DEFAULT_JOB_RETRY): the library applies its own 600 s insert
# wrapper for any non-None value.
_MAX_API_WAIT_SECONDS = 30
_API_RETRY = DEFAULT_RETRY.with_timeout(_MAX_API_WAIT_SECONDS)


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
        # `sql` is the caller's own query, passed as this tool's actual `sql` parameter -
        # wrapping their query in a LIMIT subquery to cap rows is this function's job, not
        # untrusted input reaching a query built from a different source. The trailing `;`
        # and any trailing comments go first (sql_scope.single_statement_body, shared with
        # the other engines): either makes `(...) AS x LIMIT n` a syntax error - a line
        # comment swallows the closing parenthesis.
        inner_sql = single_statement_body(sql, "bigquery")
        wrapped_sql = f"SELECT * FROM ({inner_sql}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

    job_config = (
        bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
        if max_bytes_billed is not None
        else bigquery.QueryJobConfig()
    )
    # Provider-side deadline equal to the client watchdog below. result(timeout=...) only
    # bounds the query while this process's worker thread is alive: after an MCP cancel the
    # thread is abandoned, and a host that escalates to SIGTERM/SIGKILL after closing stdin
    # (the reference MCP client does, ~2-4 s after EOF; in Docker the server is PID 1) kills
    # the watchdog with the process and the job keeps billing. jobTimeoutMs makes BigQuery
    # itself cancel the job at the same 120 s, whatever happens to this process.
    job_config.job_timeout_ms = _MAX_EXECUTION_WAIT_SECONDS * 1000
    try:
        query_job = client.query(
            wrapped_sql,
            job_config=job_config,
            retry=_API_RETRY,
            timeout=_MAX_API_WAIT_SECONDS,
            job_retry=None,
        )
    except GoogleAPICallError as exc:
        if _is_bytes_billed_limit_exceeded(exc):
            raise ProviderByteCapExceeded(max_bytes_billed) from None
        raise
    try:
        rows = [
            dict(row)
            for row in query_job.result(timeout=_MAX_EXECUTION_WAIT_SECONDS, job_retry=None)
        ]
    except GoogleAPICallError as exc:
        if _is_bytes_billed_limit_exceeded(exc):
            raise ProviderByteCapExceeded(max_bytes_billed) from None
        raise
    except concurrent.futures.TimeoutError:
        try:
            # A cancel is a BigQuery API round-trip too: bound it like every other call,
            # or an unreachable Google pins this worker thread (and its in-flight slot)
            # for the library's 600 s DEFAULT_RETRY deadline.
            query_job.cancel(retry=_API_RETRY, timeout=_MAX_API_WAIT_SECONDS)
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
        service_account_email = client.get_service_account_email(
            retry=_API_RETRY, timeout=_MAX_API_WAIT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - reporting failure as data, not raising, by design
        raw_detail = str(exc)
        safe_detail = present_remote_text(redact_secrets(raw_detail), total_chars=len(raw_detail))
        logger.warning("engine=bigquery check_credentials_failed detail=%s", safe_detail)
        return CredentialCheckResult(engine="bigquery", ok=False, detail=safe_detail)

    return CredentialCheckResult(
        engine="bigquery",
        ok=True,
        detail=(
            f"Authenticated to project '{client.project}' "
            f"(BigQuery service account: {service_account_email})."
        ),
    )
