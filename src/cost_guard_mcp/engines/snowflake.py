import json
import logging
import re
import time
import uuid

import snowflake.connector

from cost_guard_mcp.config import load_snowflake_config
from cost_guard_mcp.errors import SanitizedEngineError, redact_secrets, sanitize_exceptions
from cost_guard_mcp.pricing.runtime_scaling import scale_runtime_hours
from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour, usd_per_credit
from cost_guard_mcp.types import (
    AccuracyTier,
    CostEstimate,
    CredentialCheckResult,
    HistoricalRuntimeSignal,
)

logger = logging.getLogger(__name__)

# The connector's own defaults leave this tool exposed to indefinite hangs: login_timeout
# falls back to snowflake.connector.auth.by_plugin.DEFAULT_AUTH_CLASS_TIMEOUT (120s) only if
# unset, and network_timeout has NO fallback at all — it's infinite. A pre-flight cost-check
# tool that can hang forever on a stalled connection defeats its own purpose, so both are set
# explicitly here rather than left to those defaults.
_LOGIN_TIMEOUT_SECONDS = 30
_NETWORK_TIMEOUT_SECONDS = 60


@sanitize_exceptions("snowflake")
def _connect() -> "snowflake.connector.SnowflakeConnection":
    config = load_snowflake_config()

    if config.private_key_path:
        return snowflake.connector.connect(
            account=config.account,
            user=config.user,
            role=config.role,
            authenticator="SNOWFLAKE_JWT",
            private_key_file=config.private_key_path,
            private_key_file_pwd=config.private_key_passphrase,
            login_timeout=_LOGIN_TIMEOUT_SECONDS,
            network_timeout=_NETWORK_TIMEOUT_SECONDS,
        )

    return snowflake.connector.connect(
        account=config.account,
        user=config.user,
        role=config.role,
        password=config.password,
        login_timeout=_LOGIN_TIMEOUT_SECONDS,
        network_timeout=_NETWORK_TIMEOUT_SECONDS,
    )


_WAREHOUSE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _validate_warehouse(warehouse: str) -> str:
    if not _WAREHOUSE_NAME_RE.fullmatch(warehouse):
        raise ValueError(
            f"Invalid warehouse name {warehouse!r}: must match [A-Za-z_][A-Za-z0-9_$]*"
        )
    return warehouse


# EXPLAIN itself doesn't estimate runtime — this is a deliberately conservative, documented
# placeholder assumption (30 seconds) used only to turn a byte/partition estimate into SOME
# dollar figure. This is the least-defensible part of the Snowflake estimate; tightening it
# (e.g. by correlating bytesAssigned with historical query_history runtimes for this
# warehouse) is the highest-value follow-up work once real usage data exists.
_ASSUMED_RUNTIME_HOURS = 30 / 3600

# Warehouse-history calibration (see docs/superpowers/specs/2026-09-15-warehouse-history-
# calibration-design.md): when the caller's own INFORMATION_SCHEMA.QUERY_HISTORY has a real
# (non-cache-hit) execution of this exact SQL text, use its average runtime instead of the
# coarse byte-size-tier heuristic above. _HISTORY_LOOKUP_TIMEOUT_SECONDS bounds the lookup
# per the design spec's Global Constraints — it's passed as cur.execute()'s own `timeout`
# kwarg below, which is the connector's client-side "timebomb" (cancels the query and raises
# after N seconds); this metadata read is a single synchronous call, not a real warehouse-
# billed job awaiting async completion, so it doesn't need execute_bounded's heavier
# poll/cancel loop — the connector's own per-call timeout is the right-sized tool here.
_HISTORY_LOOKUP_TIMEOUT_SECONDS = 5
_HISTORY_LOOKUP_RESULT_LIMIT = 500


def _normalize_sql_for_matching(sql_text: str) -> str:
    """Collapse whitespace runs to single spaces and strip - deliberately NOT case-folded,
    since quoted identifiers in Snowflake are case-sensitive and case-folding risks
    conflating genuinely different queries."""
    return " ".join(sql_text.split())


def _lookup_historical_runtime(
    conn: "snowflake.connector.SnowflakeConnection", sql_text: str
) -> HistoricalRuntimeSignal | None:
    """Best-effort - any failure (permission, network, malformed/None-valued row, timeout)
    returns None, never raises; no exception propagates out of explain_estimate because of
    this lookup. Cache-hit rows (bytes_scanned == 0) are excluded - averaging in a
    near-instant cached result would corrupt calibration toward underestimating future
    runtime (confirmed live 2026-09-15: a real 360ms/10,741,184-byte execution vs. an
    immediate 54ms/0-byte cache-hit repeat).

    INFORMATION_SCHEMA.QUERY_HISTORY is per-database, not global, and this connection never
    issues its own USE DATABASE (see _connect() / load_snowflake_config() - neither sets a
    default database). Rather than requiring a new, feature-specific database context, this
    fully-qualifies the table function against the built-in `SNOWFLAKE` system database,
    which every account has and which is visible to all users by default. Per Snowflake's own
    docs, INFORMATION_SCHEMA access is not among the object types that require an explicit
    ACCOUNTADMIN-only grant (unlike ACCOUNT_USAGE/READER_ACCOUNT_USAGE/ORGANIZATION_USAGE/
    DATA_SHARING_USAGE) - but that is an inference from documentation, not a live-tested
    result against this project's actual least-privilege role (DECISIONS.md #7 requires a
    role scoped to the objects being cost-estimated, never ACCOUNTADMIN). This assumption is
    exactly what the plan's Phase 4 live-verification task is meant to confirm or correct
    before it's treated as settled; until then, if a specific role somehow lacks access, the
    surrounding try/except degrades to None exactly like any other permission failure, so a
    wrong assumption here fails safe rather than breaking explain_estimate.
    """
    normalized_target = _normalize_sql_for_matching(sql_text)
    try:
        with conn.cursor() as cur:
            # RESULT_LIMIT is our own int constant, never caller input - same class of
            # interpolation as execute_bounded's LIMIT clause below.
            history_query = (
                "SELECT QUERY_TEXT, TOTAL_ELAPSED_TIME, BYTES_SCANNED "  # noqa: S608
                "FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY("
                f"RESULT_LIMIT => {_HISTORY_LOOKUP_RESULT_LIMIT})) "
                "WHERE EXECUTION_STATUS = 'SUCCESS'"
            )
            cur.execute(history_query, timeout=_HISTORY_LOOKUP_TIMEOUT_SECONDS)
            rows = cur.fetchall()

        # Row-level type guards, not just a blanket except: a real QUERY_HISTORY row with a
        # None QUERY_TEXT or a non-numeric BYTES_SCANNED (both observed as plausible real
        # values) must be treated as a non-match, not crash the whole lookup - this whole
        # block sits inside the same try as the query above so any other malformed-response
        # shape still degrades to None rather than raising.
        matches_ms = [
            elapsed_ms
            for query_text, elapsed_ms, bytes_scanned in rows
            if isinstance(query_text, str)
            and isinstance(elapsed_ms, (int, float))
            and isinstance(bytes_scanned, (int, float))
            and bytes_scanned > 0
            and _normalize_sql_for_matching(query_text) == normalized_target
        ]
        if not matches_ms:
            return None
        avg_hours = (sum(matches_ms) / len(matches_ms)) / 1000 / 3600
        return HistoricalRuntimeSignal(avg_runtime_hours=avg_hours, sample_count=len(matches_ms))
    except Exception:  # noqa: BLE001 - best-effort, matches cancel()/close() discipline
        return None


@sanitize_exceptions("snowflake")
def explain_estimate(
    sql: str,
    warehouse: str | None,
    warehouse_size: str = "XSMALL",
    edition: str = "standard",
) -> CostEstimate:
    """Estimate Snowflake query cost via EXPLAIN. Always UPPER_BOUND — never PRECISE — since
    Snowflake bills by warehouse-time, not bytes, and EXPLAIN's byte/partition figures are
    themselves documented upper bounds ("runtime optimizations... can reduce the number of
    partitions and bytes scanned")."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if warehouse is not None:
                cur.execute(f"USE WAREHOUSE {_validate_warehouse(warehouse)}")
            cur.execute(f"EXPLAIN USING JSON {sql}")
            row = cur.fetchone()

        if row is None:
            raise ValueError("EXPLAIN USING JSON returned no rows")

        plan = json.loads(row[0])
        global_stats = plan["GlobalStats"]
        bytes_assigned = global_stats["bytesAssigned"]

        rate = credits_per_hour(warehouse_size)
        price = usd_per_credit(edition)

        historical = _lookup_historical_runtime(conn, sql)
        if historical is not None:
            runtime_hours = historical.avg_runtime_hours
            runtime_caveat = (
                f"Cost is informed by {historical.sample_count} historical run(s) of this "
                f"exact query, averaging {historical.avg_runtime_hours * 3600:.1f}s — more "
                "reliable than the coarse size-tier heuristic below."
            )
        else:
            runtime_hours = scale_runtime_hours(
                bytes_assigned, baseline_hours=_ASSUMED_RUNTIME_HOURS
            )
            runtime_caveat = (
                f"Cost assumes a baseline {int(_ASSUMED_RUNTIME_HOURS * 3600)}-second runtime "
                f"on a {warehouse_size} warehouse, scaled up by a coarse size tier based on "
                f"{bytes_assigned} bytes scanned — still a heuristic, not derived from this "
                "query's actual expected runtime."
            )
        estimated_cost_usd = rate * price * runtime_hours

        return CostEstimate(
            engine="snowflake",
            accuracy_tier=AccuracyTier.UPPER_BOUND,
            estimated_bytes=bytes_assigned,
            estimated_cost_usd=round(estimated_cost_usd, 6),
            caveats=[
                (
                    "This estimate excludes Cortex AI Function ('AI Credits') cost — EXPLAIN's "
                    "bytesAssigned only reflects warehouse compute/scan, not AI-inference calls "
                    "inside the SQL."
                ),
                runtime_caveat,
            ],
        )
    finally:
        # Best-effort close, mirroring the cancel-is-best-effort discipline in execute_bounded
        # below — a failed close must never mask the real return value/exception above.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


# dust-tt/dust's precedent for bounding a long-running warehouse job: poll every 2s, give
# up (and cancel) after 2 minutes total. Snowflake's own get_results_from_sfqid() has an
# internal retry loop, but no overall wall-clock cap — a query stuck behind slot contention
# or a suspended/cold warehouse would otherwise block this tool call indefinitely, still
# burning warehouse-seconds the whole time, which defeats the point of a "bounded" tool.
_POLL_INTERVAL_SECONDS = 2
_MAX_EXECUTION_WAIT_SECONDS = 120


def _cancel_query(conn: "snowflake.connector.SnowflakeConnection", query_id: str) -> None:
    """Best-effort cancel; a failed cancel must not mask the TimeoutError the caller raises.

    `query_id` is connector-generated (cur.sfqid), not caller input — but it still gets
    validated as a UUID before reaching raw SQL text, same defense-in-depth discipline as
    _validate_warehouse and _validate_project_id use for genuinely caller-supplied values.
    """
    try:
        uuid.UUID(query_id)
        with conn.cursor() as cur:
            cur.execute(f"SELECT SYSTEM$CANCEL_QUERY('{query_id}')")
    except Exception:  # noqa: BLE001, S110
        pass


@sanitize_exceptions("snowflake")
def execute_bounded(
    sql: str, warehouse: str | None, max_rows: int | None
) -> tuple[list[dict], int, bool]:
    conn = _connect()
    try:
        wrapped_sql = sql
        if max_rows is not None:
            # `sql` is the caller's own query, passed as this tool's actual `sql` parameter -
            # wrapping their query in a LIMIT subquery to cap rows is this function's job, not
            # untrusted input reaching a query built from a different source. Strip a trailing
            # semicolon first (matching bigquery.py's own execute_bounded) - LLM-generated SQL
            # commonly ends in one, and `SELECT * FROM (...;) AS x LIMIT n` is a syntax error.
            inner_sql = sql.strip().rstrip(";").strip()
            wrapped_sql = f"SELECT * FROM ({inner_sql}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

        with conn.cursor(snowflake.connector.DictCursor) as cur:
            if warehouse is not None:
                cur.execute(f"USE WAREHOUSE {_validate_warehouse(warehouse)}")
            cur.execute_async(wrapped_sql)
            query_id = cur.sfqid
            if query_id is None:
                raise SanitizedEngineError("execute_async did not return a query id")

            elapsed_seconds = 0
            status = conn.get_query_status(query_id)
            while conn.is_still_running(status):
                if elapsed_seconds >= _MAX_EXECUTION_WAIT_SECONDS:
                    _cancel_query(conn, query_id)
                    logger.warning(
                        "engine=snowflake execute_bounded timed out after %ss, "
                        "cancelled query_id=%s",
                        _MAX_EXECUTION_WAIT_SECONDS,
                        query_id,
                    )
                    raise TimeoutError(
                        f"Query exceeded {_MAX_EXECUTION_WAIT_SECONDS}s and was cancelled "
                        f"(query_id={query_id})."
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)
                elapsed_seconds += _POLL_INTERVAL_SECONDS
                status = conn.get_query_status(query_id)

            conn.get_query_status_throw_if_error(query_id)
            cur.get_results_from_sfqid(query_id)
            rows = cur.fetchall()

        row_cap_hit = max_rows is not None and len(rows) > max_rows
        if row_cap_hit:
            rows = rows[:max_rows]

        return rows, len(rows), row_cap_hit
    finally:
        # Outermost finally: runs on every exit path (success, timeout-raised, real-query-
        # error-reraised) after all existing cursor/cancel logic above. Best-effort, mirroring
        # _cancel_query's own discipline — a failed close must never mask the real outcome.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


def check_credentials(warehouse: str | None = None) -> CredentialCheckResult:
    """Verify Snowflake credentials/connectivity without running EXPLAIN or any real query.

    Deliberately does NOT use @sanitize_exceptions: a failed check is the expected, useful
    result here, not an error condition — this function always returns a
    CredentialCheckResult (never raises for a credential/connectivity failure) so a caller
    can distinguish "not configured yet" from "actually broken" before attempting a real
    estimate_query_cost/run_query_bounded call. redact_secrets is applied defensively even
    though _connect() already redacts its own failures — it is a no-op on already-safe text.
    """
    conn: snowflake.connector.SnowflakeConnection | None = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            if warehouse is not None:
                cur.execute(f"USE WAREHOUSE {_validate_warehouse(warehouse)}")
            cur.execute("SELECT CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_ACCOUNT()")
            row = cur.fetchone()
            if row is None:
                raise SanitizedEngineError("session context query returned no rows")
            role, current_warehouse, account = row
    except Exception as exc:  # noqa: BLE001 - reporting failure as data, not raising, by design
        return CredentialCheckResult(engine="snowflake", ok=False, detail=redact_secrets(str(exc)))
    finally:
        # conn is None if _connect() itself raised — nothing to close in that case. A failed
        # close() is best-effort and must never mask the success return below.
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass

    return CredentialCheckResult(
        engine="snowflake",
        ok=True,
        detail=(
            f"Authenticated as role '{role}' on account '{account}', "
            f"warehouse '{current_warehouse}'."
        ),
    )
