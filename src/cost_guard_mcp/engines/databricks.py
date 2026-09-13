import re
import threading

from databricks import sql
from databricks.sdk.core import Config, oauth_service_principal
from databricks.sdk.credentials_provider import OAuthCredentialsProvider

from cost_guard_mcp.config import load_databricks_config
from cost_guard_mcp.errors import redact_secrets, sanitize_exceptions
from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour
from cost_guard_mcp.types import AccuracyTier, CostEstimate, CredentialCheckResult

# databricks-sql-connector's own _socket_timeout defaults to 900s on the backend used
# here (confirmed via CONNECTION_PARAMETERS.md against the installed package) - not
# infinite, but still too long to leave implicit. Set explicitly, matching this
# project's own established discipline (see snowflake.py's _NETWORK_TIMEOUT_SECONDS).
#
# Deliberately do NOT pass use_kernel=True: the newer Rust "kernel" backend rejects a
# generic credentials_provider (OAuth M2M) with NotSupportedError. The default (legacy
# Thrift) backend used here supports both PAT and OAuth M2M.
_SOCKET_TIMEOUT_SECONDS = 60


@sanitize_exceptions("databricks")
def _connect() -> "sql.client.Connection":
    config = load_databricks_config()

    if config.client_id:
        sdk_config = Config(
            host=f"https://{config.server_hostname}",
            client_id=config.client_id,
            client_secret=config.client_secret,
        )

        def credentials_provider() -> OAuthCredentialsProvider:
            return oauth_service_principal(sdk_config)

        return sql.connect(
            server_hostname=config.server_hostname,
            http_path=config.http_path,
            credentials_provider=credentials_provider,
            _socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        )

    return sql.connect(
        server_hostname=config.server_hostname,
        http_path=config.http_path,
        access_token=config.access_token,
        _socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


def check_credentials(warehouse: str | None = None) -> CredentialCheckResult:
    """Verify Databricks credentials/connectivity without running EXPLAIN COST or any real
    query.

    `warehouse` is accepted only for calling-convention consistency with the bigquery/
    snowflake engines and is a documented no-op: Databricks has no per-query USE WAREHOUSE
    equivalent — the SQL warehouse is fixed by DATABRICKS_HTTP_PATH at connect time.

    Deliberately does NOT use @sanitize_exceptions: a failed check is the expected, useful
    result here, not an error condition — this function always returns a
    CredentialCheckResult so a caller can distinguish "not configured yet" from "actually
    broken" before attempting a real estimate_query_cost/run_query_bounded call.
    """
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT current_user()")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("SELECT current_user() returned no rows")
            current_user = row[0]
    except Exception as exc:  # noqa: BLE001 - reporting failure as data, not raising, by design
        return CredentialCheckResult(engine="databricks", ok=False, detail=redact_secrets(str(exc)))

    return CredentialCheckResult(
        engine="databricks", ok=True, detail=f"Authenticated as '{current_user}'."
    )


_UNIT_MULTIPLIERS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}

# Real confirmed EXPLAIN COST output format (docs.databricks.com/aws/en/optimizations/cbo,
# live-verified 2026-09-13): "Statistics(sizeInBytes=134.6 GB, rowCount=2.88E+9, ...)" -
# unit-suffixed, not a raw byte integer like Snowflake's bytesAssigned.
_STATISTICS_RE = re.compile(r"sizeInBytes=([\d.]+)\s*(B|KB|MB|GB|TB|PB)\b", re.IGNORECASE)

# EXPLAIN COST gives no runtime estimate at all - same deliberately conservative,
# documented placeholder pattern already used for Snowflake (see _ASSUMED_RUNTIME_HOURS
# in snowflake.py), turning a byte estimate (when available) into SOME dollar figure.
_ASSUMED_RUNTIME_HOURS = 30 / 3600


def _parse_max_size_in_bytes(explain_output: str) -> int | None:
    matches = _STATISTICS_RE.findall(explain_output)
    if not matches:
        return None
    sizes = [float(value) * _UNIT_MULTIPLIERS[unit.upper()] for value, unit in matches]
    return int(max(sizes))


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
    """
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN COST {sql_text}")
        rows = cur.fetchall()

    explain_output = "\n".join(row[0] for row in rows)
    max_size_in_bytes = _parse_max_size_in_bytes(explain_output)

    rate = dbus_per_hour(warehouse_size)
    estimated_cost_usd = rate * SERVERLESS_USD_PER_DBU * _ASSUMED_RUNTIME_HOURS

    caveats = [
        (
            "Databricks has no BigQuery-style dry-run; this estimate is HEURISTIC, the "
            "least precise of this project's three accuracy tiers."
        ),
        (
            f"Cost assumes a {int(_ASSUMED_RUNTIME_HOURS * 3600)}-second runtime on a "
            f"{warehouse_size} Serverless SQL warehouse - a rough placeholder, not derived "
            "from this query's actual expected runtime."
        ),
        (
            "Pricing assumes a Serverless SQL warehouse; Classic/Pro warehouses use "
            "different (lower) DBU rates plus a separate underlying cloud VM cost not "
            "modeled here."
        ),
    ]
    if max_size_in_bytes is None:
        caveats.append(
            "EXPLAIN COST returned no size statistics for this query (common without "
            "ANALYZE TABLE having been run, for streaming sources, or for non-Delta "
            "external tables) - the byte estimate is unavailable, not zero."
        )

    return CostEstimate(
        engine="databricks",
        accuracy_tier=AccuracyTier.HEURISTIC,
        estimated_bytes=max_size_in_bytes,
        estimated_cost_usd=round(estimated_cost_usd, 6),
        caveats=caveats,
    )


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

    conn = _connect()
    wrapped_sql = sql_text
    if max_rows is not None:
        # `sql_text` is the caller's own query, passed as this tool's actual `sql`
        # parameter - wrapping their query in a LIMIT subquery to cap rows is this
        # function's job, not untrusted input reaching a query built elsewhere.
        wrapped_sql = f"SELECT * FROM ({sql_text}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

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
        raise TimeoutError(f"Query exceeded {max_wait_seconds}s and was cancelled.")

    if execution_error:
        raise execution_error[0]

    rows = [dict(row) for row in cur.fetchall()]
    cur.close()

    row_cap_hit = max_rows is not None and len(rows) > max_rows
    if row_cap_hit:
        rows = rows[:max_rows]

    return rows, len(rows), row_cap_hit
