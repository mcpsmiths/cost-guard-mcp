import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Any

from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config, oauth_service_principal
from databricks.sdk.credentials_provider import OAuthCredentialsProvider
from databricks.sdk.service.sql import GetWarehouseResponse

from cost_guard_mcp.config import DatabricksConfig, load_databricks_config
from cost_guard_mcp.errors import (
    SanitizedEngineError,
    present_remote_text,
    redact_secrets,
    sanitize_exceptions,
)
from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour
from cost_guard_mcp.pricing.runtime_scaling import scale_runtime_hours
from cost_guard_mcp.sql_scope import single_statement_body
from cost_guard_mcp.types import AccuracyTier, CostEstimate, CredentialCheckResult

logger = logging.getLogger(__name__)

# Every connector/SDK round-trip is bounded explicitly. The libraries' own defaults are
# finite but far too long to leave implicit against the documented 120 s tool-call bound
# (same discipline as snowflake.py's _NETWORK_TIMEOUT_SECONDS). Defaults being overridden,
# confirmed against the installed databricks-sql-connector 4.6.0 / databricks-sdk 0.148.0:
#   - connector _socket_timeout: 900 s on the Thrift backend used here.
#   - connector retry policy (_retry_stop_after_attempts_duration / _count): 900 s / 30
#     attempts, read from the connect kwargs. It exists to ride out a classic/pro
#     warehouse's cold start - which is exactly how a read-only pre-flight call against an
#     unreachable workspace pinned a worker-thread slot for ~14 minutes.
#   - SDK Config (OAuth M2M only): retry_timeout_seconds 300 s / http_timeout_seconds 60 s
#     for the host-metadata probe that runs at Config() time.
#   - No server-side statement timeout at all; every session now carries STATEMENT_TIMEOUT
#     (the API ceiling on read-only paths, the execution bound in execute_bounded) so the
#     warehouse itself kills a statement this process can no longer cancel.
# Residual, OAuth M2M only, not tunable through Config (upstream databricks-sdk):
# Config.databricks_oidc_endpoints -> oauth.get_endpoints_from_url runs OIDC discovery on
# the module-default _BaseClient() (300 s retry budget), and oauth.ClientCredentials' token
# request is a requests.post without a timeout. Wrapping _connect in a thread+join watchdog
# was rejected because it would leak the connecting thread; README qualifies the 120 s
# bound for OAuth M2M accordingly. PAT auth is fully bounded.
#
# Deliberately do NOT pass use_kernel=True: the newer Rust "kernel" backend rejects a
# generic credentials_provider (OAuth M2M) with NotSupportedError. The default (legacy
# Thrift) backend used here supports both PAT and OAuth M2M.
_SOCKET_TIMEOUT_SECONDS = 60
# Ceiling for every connector/SDK retry budget and the STATEMENT_TIMEOUT of read-only
# sessions (check_credentials, explain_estimate).
_MAX_API_WAIT_SECONDS = 60
_CONNECTOR_RETRY_ATTEMPTS = 5

# Wall-clock bound for the Databricks SDK's REST calls (the warehouse lookup below and the
# SDK's own host-metadata probe during Config()); the SDK's default retry budget is 300 s.
# Equal to the connector's socket timeout so one unreachable workspace never costs a tool
# call more than about two of these before the estimate falls back.
_MAX_API_WAIT_SECONDS = 60


@sanitize_exceptions("databricks")
def _connect(statement_timeout_seconds: int = _MAX_API_WAIT_SECONDS) -> "sql.client.Connection":
    """Open a bounded connection. `statement_timeout_seconds` becomes the session's
    server-side STATEMENT_TIMEOUT; read-only callers take the API-ceiling default,
    execute_bounded passes its own execution bound."""
    config = load_databricks_config()

    # One kwargs dict for both auth branches so neither can drift back to the connector's
    # defaults. STATEMENT_TIMEOUT is in the connector's own session-conf allowlist
    # (databricks/sql/backend/sea/utils/constants.py) and is forwarded to OpenSession.
    connect_kwargs: dict[str, Any] = {
        "server_hostname": config.server_hostname,
        "http_path": config.http_path,
        "_socket_timeout": _SOCKET_TIMEOUT_SECONDS,
        "_retry_stop_after_attempts_duration": _MAX_API_WAIT_SECONDS,
        "_retry_stop_after_attempts_count": _CONNECTOR_RETRY_ATTEMPTS,
        "session_configuration": {"STATEMENT_TIMEOUT": str(statement_timeout_seconds)},
    }

    if config.client_id:
        # auth_type pins the strategy so the SDK does not also read DATABRICKS_TOKEN from the
        # environment and fail with "more than one authorization method configured" when
        # both variable sets are present.
        sdk_config = Config(
            host=f"https://{config.server_hostname}",
            client_id=config.client_id,
            client_secret=config.client_secret,
            auth_type="oauth-m2m",
            retry_timeout_seconds=_MAX_API_WAIT_SECONDS,
            http_timeout_seconds=_SOCKET_TIMEOUT_SECONDS,
        )

        def credentials_provider() -> OAuthCredentialsProvider:
            return oauth_service_principal(sdk_config)

        return sql.connect(credentials_provider=credentials_provider, **connect_kwargs)

    return sql.connect(access_token=config.access_token, **connect_kwargs)


def check_credentials(warehouse: str | None = None) -> CredentialCheckResult:
    """Verify Databricks credentials/connectivity by opening a session on the
    DATABRICKS_HTTP_PATH warehouse and executing `SELECT current_user()` - a lightweight
    context statement, not EXPLAIN COST or a real query. It does run on the warehouse, so it
    can start a STOPPED SQL warehouse and bill compute until the warehouse auto-stops again
    (whether the Thrift session alone auto-starts it is the pending owner observation in audit
    record read-only-tools-auto-start-stopped-warehouse; the fail-closed warehouse-state guard
    with its COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START opt-in is that record's open decision
    and would reuse _lookup_warehouse's `state`).

    `warehouse` is accepted only for calling-convention consistency with the bigquery/
    snowflake engines and is a documented no-op: Databricks has no per-query USE WAREHOUSE
    equivalent — the SQL warehouse is fixed by DATABRICKS_HTTP_PATH at connect time.

    Deliberately does NOT use @sanitize_exceptions: a failed check is the expected, useful
    result here, not an error condition — this function always returns a
    CredentialCheckResult so a caller can distinguish "not configured yet" from "actually
    broken" before attempting a real estimate_query_cost/run_query_bounded call.
    """
    conn: sql.client.Connection | None = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT current_user()")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("SELECT current_user() returned no rows")
            current_user = row[0]
    except Exception as exc:  # noqa: BLE001 - reporting failure as data, not raising, by design
        raw_detail = str(exc)
        safe_detail = present_remote_text(redact_secrets(raw_detail), total_chars=len(raw_detail))
        logger.warning("engine=databricks check_credentials_failed detail=%s", safe_detail)
        return CredentialCheckResult(engine="databricks", ok=False, detail=safe_detail)
    finally:
        # conn is None if _connect() itself raised — nothing to close in that case. A failed
        # close() is best-effort and must never mask the success return below.
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass

    # current_user() is warehouse-returned text and this detail reaches the model as
    # structuredContent, so it is presented (bounded, repr-quoted, one line) rather than
    # interpolated raw inside literal quotes - PR #86 verification (probe-4 p5c) delivered a
    # detail carrying U+000A, ESC, CR, U+0085 and U+2028 through the old f-string. The
    # success path stays log-free.
    return CredentialCheckResult(
        engine="databricks",
        ok=True,
        detail=f"Authenticated as {present_remote_text(str(current_user))}.",
    )


# Every size label Spark's Statistics formatter can print, upper-cased for lookup. Both
# families are 1024-based: Spark's Utils.bytesToString divides by 1024 whether it labels
# the result "GB" (Spark 2.x) or "GiB" (Spark 3.x), so "134.6 GB" and "134.6 GiB" are the
# same byte count here.
_UNIT_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "KIB": 1024,
    "MB": 1024**2,
    "MIB": 1024**2,
    "GB": 1024**3,
    "GIB": 1024**3,
    "TB": 1024**4,
    "TIB": 1024**4,
    "PB": 1024**5,
    "PIB": 1024**5,
    "EB": 1024**6,
    "EIB": 1024**6,
}

# EXPLAIN COST prints unit-suffixed sizes, not a raw byte integer like Snowflake's
# bytesAssigned: "Statistics(sizeInBytes=134.6 GB, rowCount=2.88E+9, hints=none)". The SI
# label shape comes from the docs.databricks.com/aws/en/optimizations/cbo example (the
# Spark 2.x Statistics.toString form, fetched 2026-09-13); the IEC shape ("134.6 GiB", no
# hints=) is what upstream Spark 3.x Utils.bytesToString emits. No EXPLAIN COST output has
# yet been captured from a live warehouse, so both label families are accepted and a
# leaf-table size is never silently skipped because of a suffix mismatch (audit:
# databricks-explain-cost-size-regex-rejects-iec-unit-suffixes).
_STATISTICS_RE = re.compile(
    r"sizeInBytes=([\d.]+)\s*(B|KiB|MiB|GiB|TiB|PiB|EiB|KB|MB|GB|TB|PB|EB)\b", re.IGNORECASE
)

# Spark defaults a relation's statistics to Long.MaxValue (2**63 - 1) when it has none and
# prints that as "8.0 EB"/"8.0 EiB". It encodes "unknown", not a scan size, so it is dropped
# before max() - otherwise it would both outrank every real leaf and stand in as a 9.2e18-
# byte estimate on its own instead of the honest None that refuses both caps.
_SPARK_UNKNOWN_SIZE_SENTINEL_BYTES = 2**63 - 1

# EXPLAIN COST gives no runtime estimate at all - same deliberately conservative,
# documented placeholder pattern already used for Snowflake (see _ASSUMED_RUNTIME_HOURS
# in snowflake.py), turning a byte estimate (when available) into SOME dollar figure.
_ASSUMED_RUNTIME_HOURS = 30 / 3600


def _parse_max_size_in_bytes(explain_output: str) -> int | None:
    matches = _STATISTICS_RE.findall(explain_output)
    sizes = [float(value) * _UNIT_MULTIPLIERS[unit.upper()] for value, unit in matches]
    known_sizes = [size for size in sizes if size < _SPARK_UNKNOWN_SIZE_SENTINEL_BYTES]
    if not known_sizes:
        return None
    return int(max(known_sizes))


# DATABRICKS_HTTP_PATH names the SQL warehouse the connector binds to; its id is the last path
# segment and is interpolated into the SDK's GET /api/2.0/sql/warehouses/{id} call, so it gets
# the same allowlist treatment as every other interpolated identifier (DECISIONS.md #6).
_WAREHOUSE_ID_RE = re.compile(r"^/sql/1\.0/warehouses/([A-Za-z0-9]+)$")


def _warehouse_id(http_path: str) -> str:
    match = _WAREHOUSE_ID_RE.fullmatch(http_path)
    if match is None:
        raise ValueError(
            "DATABRICKS_HTTP_PATH is not a SQL warehouse path of the form "
            "/sql/1.0/warehouses/<warehouse-id>; the warehouse cannot be looked up."
        )
    return match.group(1)


@dataclass(frozen=True)
class _WarehouseFacts:
    """What GET /api/2.0/sql/warehouses/{id} says about the http_path warehouse. `state` is
    carried for the pending read-only warehouse-state guard (audit record read-only-tools-
    auto-start-stopped-warehouse) so it can share this one bounded call."""

    state: str | None
    cluster_size: str | None
    is_serverless: bool


@sanitize_exceptions("databricks")
def _get_warehouse(config: DatabricksConfig) -> GetWarehouseResponse:
    warehouse_id = _warehouse_id(config.http_path)
    # auth_type is pinned on both paths so a stray DATABRICKS_TOKEN or DATABRICKS_CLIENT_ID in
    # the environment cannot make the SDK raise "more than one authorization method
    # configured" (verified against databricks-sdk 0.148.0). retry_timeout_seconds /
    # http_timeout_seconds bound both the GET and the SDK's own /.well-known/databricks-config
    # host-metadata probe during Config(); the SDK's default budget is 300 s. WorkspaceClient
    # itself does not accept those two kwargs, hence the explicit Config.
    if config.client_id:
        sdk_config = Config(
            host=f"https://{config.server_hostname}",
            client_id=config.client_id,
            client_secret=config.client_secret,
            auth_type="oauth-m2m",
            retry_timeout_seconds=_MAX_API_WAIT_SECONDS,
            http_timeout_seconds=_SOCKET_TIMEOUT_SECONDS,
        )
    else:
        sdk_config = Config(
            host=f"https://{config.server_hostname}",
            token=config.access_token,
            auth_type="pat",
            retry_timeout_seconds=_MAX_API_WAIT_SECONDS,
            http_timeout_seconds=_SOCKET_TIMEOUT_SECONDS,
        )
    return WorkspaceClient(config=sdk_config).warehouses.get(warehouse_id)


def _lookup_warehouse(config: DatabricksConfig) -> _WarehouseFacts | None:
    """Best-effort observation of the SQL warehouse DATABRICKS_HTTP_PATH binds to, through the
    already-depended-on databricks-sdk, used by explain_estimate() to bind the cap-gate rate to
    the warehouse that will actually execute instead of to the caller's warehouse_size string
    (DECISIONS.md #19). Any failure - a non-warehouse http_path, a principal without
    CAN_VIEW/CAN_USE on the warehouse, network, timeout - returns None (the sanitized reason is
    logged by @sanitize_exceptions) and explain_estimate keeps the caller-asserted rate with a
    caveat. Worst case is about 2 x _MAX_API_WAIT_SECONDS (host-metadata probe + GET); the
    OAuth path's OIDC discovery keeps the SDK's own budget (an accepted residual, see the
    databricks-readonly-paths-inherit-connector-900s-retry-duration audit record).
    """
    try:
        response = _get_warehouse(config)
    except SanitizedEngineError:
        return None
    state_value = getattr(response.state, "value", None)
    return _WarehouseFacts(
        state=state_value if isinstance(state_value, str) else None,
        cluster_size=response.cluster_size if isinstance(response.cluster_size, str) else None,
        is_serverless=response.enable_serverless_compute is True,
    )


_FAIL_CLOSED_HINT = (
    "no dollar estimate is produced, so run_query_bounded refuses a max_estimated_cost_usd cap "
    "for it; omit the dollar cap to run on this warehouse."
)


def _rate_for(
    facts: _WarehouseFacts | None, warehouse_size: str
) -> tuple[float | None, str, list[str]]:
    """Pick the DBU/h rate the cap gate prices at: (rate, effective size label, caveats).

    The rate is never below the observed warehouse - the caller's warehouse_size may raise the
    size, never lower it. `rate` is None when the warehouse is not a Serverless SQL warehouse
    (Classic/Pro add a separate cloud VM cost this project does not model) or reports a size the
    rate table lacks; explain_estimate then returns estimated_cost_usd=None so run_query_bounded
    refuses a dollar cap (DECISIONS.md #5). A failed lookup (facts is None) keeps the
    caller-asserted/default size with an explicit caveat so principals without CAN_VIEW/CAN_USE
    on the warehouse keep working (DECISIONS.md #19).
    """
    asserted_rate = dbus_per_hour(warehouse_size)
    if facts is None:
        caveat = (
            "The configured SQL warehouse could not be observed (DATABRICKS_HTTP_PATH is not a "
            "/sql/1.0/warehouses/<id> path, or GET /api/2.0/sql/warehouses/<id> failed - e.g. "
            "the principal lacks CAN_VIEW/CAN_USE on it) — priced at the caller-asserted "
            f"{warehouse_size} size assuming a Serverless SQL warehouse; the real cost is higher "
            "if the warehouse is larger, and Classic/Pro warehouses add a separate cloud VM cost "
            "not modeled here."
        )
        return asserted_rate, warehouse_size, [caveat]
    if not facts.is_serverless:
        caveat = (
            "The configured SQL warehouse is not a Serverless SQL warehouse (Classic/Pro use "
            "different DBU rates plus a separate cloud VM cost this project does not model) — "
            f"{_FAIL_CLOSED_HINT}"
        )
        return None, warehouse_size, [caveat]
    if facts.cluster_size is None:
        caveat = (
            "The configured SQL warehouse reports no cluster size this project's DBU rate table "
            f"can price — {_FAIL_CLOSED_HINT}"
        )
        return None, warehouse_size, [caveat]
    # cluster_size is API-returned text: it reaches a caveat only through present_remote_text
    # (bounded, repr-quoted, one line - DECISIONS.md #18), never as repr(...)[:N]. PR #86
    # verification (probe-4 p5e) showed the slice-after-repr form dropping the closing quote
    # on a long value, which erased the visible boundary between the warehouse's text and this
    # server's own words. The size LABEL handed back for the runtime caveat is the one value
    # that may stay raw: it is only ever a cluster_size dbus_per_hour accepted, i.e. a rate-
    # table key modulo case.
    presented_cluster_size = present_remote_text(facts.cluster_size)
    try:
        observed_rate = dbus_per_hour(facts.cluster_size)
    except ValueError:
        caveat = (
            f"The configured SQL warehouse reports cluster size {presented_cluster_size}, "
            f"which this project's DBU rate table does not model — {_FAIL_CLOSED_HINT}"
        )
        return None, warehouse_size, [caveat]
    if observed_rate > asserted_rate:
        caveat = (
            f"warehouse_size {warehouse_size!r} understated the configured Serverless SQL "
            f"warehouse ({presented_cluster_size}) — priced at the observed size; the "
            "warehouse_size argument can only raise the estimate, never lower it."
        )
        return observed_rate, facts.cluster_size, [caveat]
    caveat = (
        f"Priced at the caller-asserted {warehouse_size} size, which is at least the observed "
        f"Serverless SQL warehouse size ({presented_cluster_size})."
    )
    return asserted_rate, warehouse_size, [caveat]


@sanitize_exceptions("databricks")
def explain_estimate(
    sql_text: str, warehouse: str | None, warehouse_size: str = "X-Small"
) -> CostEstimate:
    """Estimate Databricks query cost via EXPLAIN COST. Always HEURISTIC - never PRECISE
    or UPPER_BOUND - since Databricks has no dry-run and EXPLAIN COST's plan-node
    statistics are frequently absent (no ANALYZE TABLE, streaming sources, non-Delta
    external tables).

    `warehouse` is accepted only for calling-convention consistency and is a documented
    no-op - see check_credentials's docstring for why.

    The dollar figure is priced at the SQL warehouse DATABRICKS_HTTP_PATH binds to, as
    observed through the Warehouses API (cluster size, Serverless or not) - never below it:
    `warehouse_size` can only raise the estimate. Non-Serverless (Classic/Pro) warehouses and
    unrecognised sizes yield estimated_cost_usd=None so run_query_bounded fails closed on a
    dollar cap (DECISIONS.md #5, #19).
    """
    # Validate the caller-asserted size before any lookup or connection: an unknown size is a
    # clear (sanitized) ValueError that never costs an API call.
    dbus_per_hour(warehouse_size)
    config = load_databricks_config()
    rate, size_label, rate_caveats = _rate_for(_lookup_warehouse(config), warehouse_size)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN COST {sql_text}")
            rows = cur.fetchall()

        explain_output = "\n".join(row[0] for row in rows)
        max_size_in_bytes = _parse_max_size_in_bytes(explain_output)

        caveats = [
            (
                "Databricks has no BigQuery-style dry-run; this estimate is HEURISTIC, the "
                "least precise of this project's three accuracy tiers."
            ),
            *rate_caveats,
        ]
        # A dollar figure derived from no plan statistics would depend only on the
        # warehouse rate, not on the query (0.035 USD for every X-Small query) - a cap
        # compared against it looks enforced but never discriminates. Report it as
        # unavailable instead so run_query_bounded's fail-closed gate refuses a requested
        # max_estimated_cost_usd, exactly as it already does for the byte cap when
        # estimated_bytes is None (DECISIONS.md #5). The same None results when the observed
        # warehouse has no modelled rate (rate is None, see _rate_for).
        estimated_cost_usd: float | None
        if max_size_in_bytes is None:
            estimated_cost_usd = None
            caveats.append(
                "EXPLAIN COST returned no size statistics for this query (common without "
                "ANALYZE TABLE having been run, for streaming sources, or for non-Delta "
                "external tables) - the byte and dollar estimates are unavailable, not zero; "
                "run_query_bounded will refuse any max_bytes_billed or max_estimated_cost_usd "
                "cap for this query."
            )
        else:
            runtime_hours = scale_runtime_hours(
                max_size_in_bytes, baseline_hours=_ASSUMED_RUNTIME_HOURS
            )
            estimated_cost_usd = (
                None if rate is None else round(rate * SERVERLESS_USD_PER_DBU * runtime_hours, 6)
            )
            caveats.append(
                f"Cost assumes a baseline {int(_ASSUMED_RUNTIME_HOURS * 3600)}-second runtime on "
                f"a {size_label} Serverless SQL warehouse, scaled up by a coarse size tier "
                f"based on {max_size_in_bytes} bytes scanned - still a heuristic, not derived "
                "from this query's actual expected runtime."
            )

        return CostEstimate(
            engine="databricks",
            accuracy_tier=AccuracyTier.HEURISTIC,
            estimated_bytes=max_size_in_bytes,
            estimated_cost_usd=estimated_cost_usd,
            caveats=caveats,
        )
    finally:
        # Best-effort close, mirroring the cancel-is-best-effort discipline in execute_bounded
        # below — a failed close must never mask the real return value/exception above.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


# dust-tt/dust's precedent for bounding a long-running warehouse job, already applied to
# Snowflake in this project (PR #22): give up after 2 minutes. Unlike Snowflake, there is
# no native async/polling execute mode here (confirmed via research) - execute() runs in
# a background thread, joined with this timeout, and Cursor.cancel() is called if it is
# still alive. cancel() is confirmed callable from a different thread than the one that
# called execute(), per the connector's own source docstring.
_MAX_EXECUTION_WAIT_SECONDS = 120


@sanitize_exceptions("databricks")
def execute_bounded(
    sql_text: str,
    warehouse: str | None,
    max_rows: int | None,
    *,
    _max_wait_seconds: float | None = None,
) -> tuple[list[dict], int, bool]:
    """Execute `sql_text` with an optional row bound and a wall-clock execution cap.

    `warehouse` is accepted only for calling-convention consistency and is a documented
    no-op - see check_credentials's docstring for why.

    `_max_wait_seconds` is a private, test-only override for `_MAX_EXECUTION_WAIT_SECONDS`.
    It exists because patching the module-level constant from a test needs a target
    `unittest.mock.patch` can actually resolve - `execute_bounded.__wrapped__.__globals__`
    (reachable since `sanitize_exceptions` uses `functools.wraps`) is a real dict, but
    `mock.patch`'s dotted-path resolver can't parse a bracketed subscript like
    `__globals__["_MAX_EXECUTION_WAIT_SECONDS"]` as a target string (confirmed: it raises
    AttributeError, not a successful patch) - so callers needing a shorter wait pass this
    keyword instead. Not part of the calling convention shared with bigquery/snowflake.
    """
    max_wait_seconds = (
        _max_wait_seconds if _max_wait_seconds is not None else _MAX_EXECUTION_WAIT_SECONDS
    )

    # Server-side ceiling equal to the client watchdog: the watchdog still fires first in the
    # normal case; STATEMENT_TIMEOUT matters when this process dies before it can cancel.
    conn = _connect(statement_timeout_seconds=max(1, math.ceil(max_wait_seconds)))
    try:
        wrapped_sql = sql_text
        if max_rows is not None:
            # `sql_text` is the caller's own query, passed as this tool's actual `sql`
            # parameter - wrapping their query in a LIMIT subquery to cap rows is this
            # function's job, not untrusted input reaching a query built elsewhere. The
            # trailing `;` and any trailing comments go first (sql_scope.single_statement_body,
            # shared with the other engines): either makes `(...) AS x LIMIT n` a syntax error.
            inner_sql = single_statement_body(sql_text, "databricks")
            wrapped_sql = f"SELECT * FROM ({inner_sql}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

        cur = conn.cursor()
        execution_error: list[BaseException] = []

        def _run() -> None:
            try:
                cur.execute(wrapped_sql)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
                execution_error.append(exc)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=max_wait_seconds)

        if thread.is_alive():
            try:
                cur.cancel()
            except Exception:  # noqa: BLE001, S110 - best-effort; the TimeoutError below matters
                pass
            logger.warning(
                "engine=databricks execute_bounded timed out after %ss, cancelled",
                max_wait_seconds,
            )
            raise TimeoutError(f"Query exceeded {max_wait_seconds}s and was cancelled.")

        if execution_error:
            raise execution_error[0]

        # Unlike bigquery.Row (which has a .keys() method dict() detects and uses), this
        # connector's row type is a plain tuple with no mapping protocol - dict(row) raises
        # "cannot convert dictionary update sequence element #0 to a sequence" (confirmed via
        # a live run against a real warehouse, not just unit tests). Build dicts from
        # cursor.description instead, which is guaranteed by DB API 2.0 regardless of row type.
        columns = [col[0] for col in cur.description] if cur.description else []
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        cur.close()

        row_cap_hit = max_rows is not None and len(rows) > max_rows
        if row_cap_hit:
            rows = rows[:max_rows]

        return rows, len(rows), row_cap_hit
    finally:
        # Outermost finally: runs on every exit path (success, timeout-raised, real-query-
        # error-reraised) after all existing cursor/cancel logic above. Best-effort, mirroring
        # cur.cancel()'s own discipline — a failed close must never mask the real outcome.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
