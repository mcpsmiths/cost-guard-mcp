import concurrent.futures
import logging
import re

from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.cloud import bigquery, bigquery_reservation_v1
from google.cloud.bigquery.retry import DEFAULT_RETRY

from cost_guard_mcp.engines.bigquery_estimate_rules import (
    BQ_ACCURACY_TO_TIER,
    BQ_LOWER_BOUND,
    DEFAULT_RESERVATION_LOCATION,
    USE_BYTE_CAP_INSTEAD,
    job_location,
    references_remote_billing,
    validate_location,
)
from cost_guard_mcp.errors import (
    SanitizedEngineError,
    present_remote_text,
    redact_secrets,
    sanitize_exceptions,
)
from cost_guard_mcp.pricing.bigquery_pricing import ON_DEMAND_USD_PER_TIB, TIB_IN_BYTES
from cost_guard_mcp.result_bounds import FETCH_PAGE_SIZE, collect_rows
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
def is_capacity_billed(project: str, location: str = DEFAULT_RESERVATION_LOCATION) -> bool:
    """Return True if `project` has a BigQuery Reservation assignment (Editions/capacity billing)
    in `location`.

    SearchAllAssignments is per location (the installed client documents it as a lookup "for a
    particular region"; the parent's only wildcard is the project), so no single call covers
    every location. dry_run passes the location of the dry-run job - the location whose slots
    the query would consume - instead of the fixed "US" it used before #96.
    """
    project = _validate_project_id(project)
    # Service-returned text entering a hand-built resource path: allowlisted like the project id.
    location = validate_location(location)
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


def _log_billing_model_check(location: str, location_source: str, outcome: str) -> None:
    # The location is service-returned text (DECISIONS.md #18): presented, never raw.
    logger.info(
        "engine=bigquery dry_run billing_model_check location=%s location_source=%s outcome=%s",
        present_remote_text(location),
        location_source,
        outcome,
    )


@sanitize_exceptions("bigquery")
def dry_run(sql: str, project: str | None = None) -> CostEstimate:
    """Estimate BigQuery query cost via a dry run. Tagged PRECISE unless BigQuery's own
    totalBytesProcessedAccuracy says otherwise. No dollar figure when the byte figure cannot
    be priced - a LOWER_BOUND accuracy, the row-level-security shape (0 bytes on a statement
    that references a table), a capacity-billed project or a failed billing-model check - so a
    dollar cap in run_query_bounded refuses (DECISIONS.md #5, #22; #96). The byte figure is
    always returned as reported: max_bytes_billed is enforced by BigQuery itself."""
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
    tier = BQ_ACCURACY_TO_TIER.get(raw_accuracy, AccuracyTier.UPPER_BOUND)
    # Service-returned text: bounded and repr-quoted like every other remote string that
    # reaches the model (PR #86 verification, group-B review).
    presented_accuracy = present_remote_text(str(raw_accuracy))

    caveats: list[str] = []
    has_priceable_bytes = True
    if raw_accuracy == BQ_LOWER_BOUND:
        has_priceable_bytes = False
        caveats.append(
            f"BigQuery reported this estimate's own accuracy as {presented_accuracy}: the byte "
            "figure is a floor on what the query would process, not a cap, so it is tagged "
            "HEURISTIC and no dollar figure is returned (estimated_cost_usd is null); a "
            f"max_estimated_cost_usd cap refuses. {USE_BYTE_CAP_INSTEAD}"
        )
    elif tier != AccuracyTier.PRECISE:
        caveats.append(
            f"BigQuery reported this estimate's own accuracy as {presented_accuracy}, not "
            "PRECISE — treating it conservatively as UPPER_BOUND."
        )

    # BigQuery's dry-run response always reports 0 bytes processed for tables protected by
    # row-level security, by design, to prevent a side-channel that would let a query's byte
    # cost reveal information about rows the caller can't see (Google: "dry runs always return
    # 0 bytes to prevent side-channel attacks"). Until #96 that 0 was tagged PRECISE and priced
    # at $0.00, which passed any dollar cap. The shape - 0 (or absent) bytes on a statement that
    # references at least one table - now keeps the reported 0 bytes, is tagged HEURISTIC (the
    # hidden figure is no estimate of the bytes the query would read) and carries no dollar
    # figure, so the dollar gate refuses. A statement that references no table (SELECT 1)
    # legitimately processes 0 bytes and is left alone. Accepted false positives, in the safe
    # direction (DECISIONS.md #22): the two fields read here cannot tell the RLS shape from a
    # 0-byte dry run that is genuinely free - an unfiltered COUNT(*) BigQuery answers from table
    # metadata, an empty table, a partition filter that prunes every partition - so those are
    # treated the same way (a dollar cap refuses; max_bytes_billed runs them), and the caveat
    # states the shape and lists the known causes instead of asserting row-level security.
    if total_bytes_processed == 0 and query_job.referenced_tables:
        has_priceable_bytes = False
        tier = AccuracyTier.HEURISTIC
        caveats.append(
            "This dry run reported 0 bytes processed on a statement that references a table — "
            "the shape BigQuery produces by design for tables protected by row-level security "
            "(dry runs always report 0 bytes there, to prevent a side-channel), and also for "
            "statements it answers from table metadata, such as an unfiltered COUNT(*), or over "
            "no data (an empty table, a partition filter that prunes every partition) — so the "
            "$0.00 it implies must not be trusted as proof the query is free to run: no dollar "
            "figure is returned (estimated_cost_usd is null) and a max_estimated_cost_usd cap "
            f"refuses. {USE_BYTE_CAP_INSTEAD}"
        )

    if references_remote_billing(sql):
        caveats.append(
            "This query appears to invoke a BigQuery remote function or ML.GENERATE_TEXT "
            "(BigQuery ML remote-model inference) — both incur separate Cloud Run/Vertex AI "
            "billing that this byte-based dollar estimate does not include."
        )

    # #96: the billing model is looked up for the location the job would run in. The log
    # record and the caveats below say which location was checked and why.
    location, location_source = job_location(query_job)
    if location_source == "fallback":
        caveats.append(
            "BigQuery did not report a location for this dry-run job, so the billing-model "
            "check used the US multi-region; a reservation assignment in another location "
            "would not have been seen, and any on-demand dollar figure in this estimate rests "
            "on that assumption."
        )
    capacity_billed: bool | None
    try:
        # Coerced: the function returns any(...) today, but the logged outcome and the pricing
        # decision below must agree for whatever truthy/falsy value a patched or changed
        # implementation returns, instead of a lookup failure failing the whole dry run.
        capacity_billed = bool(is_capacity_billed(client.project, location))
    except SanitizedEngineError:
        capacity_billed = None
        caveats.append(
            "Could not verify BigQuery billing model (Reservations API call failed for "
            f"location {present_remote_text(location)}) - returning a byte estimate only, no "
            "dollar figure, to avoid reporting a possibly-wrong-billing-model dollar amount."
        )
    if capacity_billed is None:
        outcome = "check_failed"
    elif capacity_billed:
        outcome = "capacity_billed"
    else:
        outcome = "on_demand"
    _log_billing_model_check(location, location_source, outcome)
    if capacity_billed:
        caveats.append(
            "This project is on BigQuery Editions/capacity billing (slot-hours) — a reservation "
            f"assignment was found in location {present_remote_text(location)} — which has no "
            "fixed $/byte rate, so no dollar estimate is possible from bytes alone."
        )

    estimated_cost_usd: float | None = None
    if has_priceable_bytes and capacity_billed is False:
        estimated_cost_usd = round(
            (total_bytes_processed / TIB_IN_BYTES) * ON_DEMAND_USD_PER_TIB, 6
        )
    return CostEstimate(
        engine="bigquery",
        accuracy_tier=tier,
        estimated_bytes=total_bytes_processed,
        estimated_cost_usd=estimated_cost_usd,
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


# How long to wait for a bounded query to finish before giving up and cancelling it. It is
# the polling deadline QueryJob.result() evaluates between is_job_done() returns, and the
# per-request transport timeout it passes to each jobs.get / jobs.getQueryResults call. It
# does NOT bound the retries inside those calls - those come from `retry=`, which is why
# result() receives _API_RETRY below (issue #103). Without the deadline a query stuck behind
# slot contention or a cold warehouse could block a tool call indefinitely, defeating the
# point of a "bounded" execution tool. Worst case for the result phase: one polling call
# that hangs for the per-request timeout (120 s, a black-holed connection) plus 30 s of
# retries, instead of the 600-720 s before #103.
_MAX_EXECUTION_WAIT_SECONDS = 120

# Bound on every *other* BigQuery API round-trip this module makes - the service-account
# lookup, the dry-run and execution job submissions (jobs.insert), the Reservations search,
# and the polling RPCs inside result(). Without an explicit retry/timeout each of those
# inherits google-cloud-bigquery's
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


def _cancel_best_effort(query_job: bigquery.QueryJob) -> None:
    """Ask BigQuery to cancel a job whose result wait was abandoned; never raises.

    A cancel is a BigQuery API round-trip too: bound it like every other call, or an
    unreachable Google pins this worker thread (and its in-flight slot) for the library's
    600 s DEFAULT_RETRY deadline. The caller's own error is the message that matters; a
    failed cancel must not mask it.
    """
    try:
        query_job.cancel(retry=_API_RETRY, timeout=_MAX_API_WAIT_SECONDS)
    except Exception:  # noqa: BLE001, S110 - best-effort, see docstring
        pass


@sanitize_exceptions("bigquery")
def execute_bounded(
    sql: str,
    max_bytes_billed: int | None,
    max_rows: int | None,
    project: str | None = None,
    *,
    wrap_in_limit: bool = True,
) -> tuple[list[dict], int, bool]:
    """Execute `sql` with optional byte and row bounds.

    Row bounding (#94): when `wrap_in_limit` (the tool passes the outcome of
    sql_scope_keywords.is_single_query_statement; library callers get today's behaviour) the
    query runs inside `LIMIT max_rows + 1`, so the result the service computes is bounded too.
    In every case the fetch asks the API for at most max_rows + 1 rows in pages of
    FETCH_PAGE_SIZE and stops consuming (result_bounds.collect_rows) at the sentinel row or
    once the running size estimate crosses MAX_RESULT_BYTES. The returned bool is True whenever
    the fetch stopped before the result was exhausted - "there were exactly max_rows" and
    "there were more" stay distinguishable through the sentinel row.
    """
    client = bigquery.Client(project=project)

    wrapped_sql = sql
    if max_rows is not None and wrap_in_limit:
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
        iterator = query_job.result(
            timeout=_MAX_EXECUTION_WAIT_SECONDS,
            # Every RPC inside result() - jobs.get via done(), jobs.getQueryResults via
            # _reload_query_results(), row paging - must carry the 30 s bound too: without
            # retry= they inherit DEFAULT_RETRY's 600 s deadline, and the timeout above is
            # only evaluated between those retried calls (issue #103). job_retry stays
            # None: it alone decides whether the library re-submits the billed job.
            retry=_API_RETRY,
            job_retry=None,
            # max_results stops the iterator at the sentinel row; page_size also bounds the
            # very first jobs.getQueryResults download (without it the API's default page
            # arrives before our code sees a row). Each REST page is further capped by the
            # API's ~10 MB response limit. Scripts return an _EmptyRowIterator that ignores
            # both. max_rows + 1 fits int64: the tool validated max_rows <= 2^63 - 2.
            max_results=None if max_rows is None else max_rows + 1,
            page_size=FETCH_PAGE_SIZE if max_rows is None else min(FETCH_PAGE_SIZE, max_rows + 1),
        )
        # The paging RPCs happen while iterating, so the consume stays inside this try.
        rows, row_cap_hit = collect_rows(iterator, max_rows)
    except GoogleAPICallError as exc:
        if _is_bytes_billed_limit_exceeded(exc):
            raise ProviderByteCapExceeded(max_bytes_billed) from None
        raise
    except (concurrent.futures.TimeoutError, RetryError) as exc:
        _cancel_best_effort(query_job)
        # The job id came back from the service, so it is remote text like any error
        # message: present it (one line, control characters escaped) before it reaches the
        # log or the exception, or a newline in it starts a prefix-free stderr record.
        presented_job_id = present_remote_text(str(query_job.job_id))
        if isinstance(exc, RetryError):
            # The polling RPCs exhausted their 30 s retry bound (a Google-side outage after
            # submission). RetryError is a GoogleAPIError, not a GoogleAPICallError, so it
            # used to skip this branch and the cancel: a getQueryResults 5xx storm does not
            # mean jobs.cancel is unreachable, and one bounded cancel RPC is cheap against up
            # to jobTimeoutMs of billing. Re-raised as-is: sanitize_exceptions presents the
            # library's own "Timeout of 30.0s exceeded" text as quoted, labelled data.
            logger.warning(
                "engine=bigquery execute_bounded result polling retries exhausted after %ss, "
                "cancelled job_id=%s",
                _MAX_API_WAIT_SECONDS,
                presented_job_id,
            )
            raise
        logger.warning(
            "engine=bigquery execute_bounded timed out after %ss, cancelled job_id=%s",
            _MAX_EXECUTION_WAIT_SECONDS,
            presented_job_id,
        )
        raise TimeoutError(
            f"Query exceeded {_MAX_EXECUTION_WAIT_SECONDS}s and was cancelled "
            f"(job_id={presented_job_id})."
        ) from None

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

    # Both identifiers came back from the service: present them like the failure path
    # presents its error text (bounded, repr-quoted, control characters escaped) instead of
    # interpolating them raw, so a line boundary or terminal sequence in either cannot reach
    # the model's structuredContent as-is. The repr supplies the quotes.
    return CredentialCheckResult(
        engine="bigquery",
        ok=True,
        detail=(
            # Same redact -> present chain as the failure path two lines above, so the two
            # branches cannot drift apart.
            f"Authenticated to project {present_remote_text(redact_secrets(str(client.project)))} "
            "(BigQuery service account: "
            f"{present_remote_text(redact_secrets(str(service_account_email)))})."
        ),
    )
