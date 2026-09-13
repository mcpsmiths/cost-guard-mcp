import json
import re
import time
import uuid

import snowflake.connector

from cost_guard_mcp.config import load_snowflake_config
from cost_guard_mcp.errors import SanitizedEngineError, redact_secrets, sanitize_exceptions
from cost_guard_mcp.pricing.snowflake_pricing import credits_per_hour, usd_per_credit
from cost_guard_mcp.types import AccuracyTier, CostEstimate, CredentialCheckResult

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
    estimated_cost_usd = rate * price * _ASSUMED_RUNTIME_HOURS

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
            (
                f"Cost assumes a {int(_ASSUMED_RUNTIME_HOURS * 3600)}-second runtime on a "
                f"{warehouse_size} warehouse — a rough placeholder, not derived from this "
                "query's actual expected runtime."
            ),
        ],
    )


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
    wrapped_sql = sql
    if max_rows is not None:
        # `sql` is the caller's own query, passed as this tool's actual `sql` parameter -
        # wrapping their query in a LIMIT subquery to cap rows is this function's job, not
        # untrusted input reaching a query built from a different source.
        wrapped_sql = f"SELECT * FROM ({sql}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

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


def check_credentials(warehouse: str | None = None) -> CredentialCheckResult:
    """Verify Snowflake credentials/connectivity without running EXPLAIN or any real query.

    Deliberately does NOT use @sanitize_exceptions: a failed check is the expected, useful
    result here, not an error condition — this function always returns a
    CredentialCheckResult (never raises for a credential/connectivity failure) so a caller
    can distinguish "not configured yet" from "actually broken" before attempting a real
    estimate_query_cost/run_query_bounded call. redact_secrets is applied defensively even
    though _connect() already redacts its own failures — it is a no-op on already-safe text.
    """
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

    return CredentialCheckResult(
        engine="snowflake",
        ok=True,
        detail=(
            f"Authenticated as role '{role}' on account '{account}', "
            f"warehouse '{current_warehouse}'."
        ),
    )
