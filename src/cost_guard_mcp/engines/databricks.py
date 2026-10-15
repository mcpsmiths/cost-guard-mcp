import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config, oauth_service_principal
from databricks.sdk.credentials_provider import OAuthCredentialsProvider
from databricks.sdk.service.sql import GetWarehouseResponse

from cost_guard_mcp.config import (
    DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR,
    DatabricksConfig,
    databricks_warehouse_start_allowed,
    load_databricks_config,
)
from cost_guard_mcp.errors import (
    SanitizedEngineError,
    present_remote_text,
    redact_secrets,
    sanitize_exceptions,
)
from cost_guard_mcp.pricing.databricks_pricing import SERVERLESS_USD_PER_DBU, dbus_per_hour
from cost_guard_mcp.pricing.runtime_scaling import scale_runtime_hours
from cost_guard_mcp.result_bounds import FETCH_PAGE_SIZE, collect_rows, iter_fetchmany
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
    context statement, not EXPLAIN COST or a real query. The session itself is what can start
    a STOPPED Classic/Pro SQL warehouse and bill compute until it auto-stops again, so by
    default (DECISIONS.md #20, issue #91) the warehouse's state is read first through the
    Warehouses API (`_lookup_warehouse`: GET /api/2.0/sql/warehouses/{id}, a control-plane call
    that does not start the warehouse) and the session is opened only when it is RUNNING.
    Every other state, and an unreadable state, returns ok=False with no session (fail closed).
    COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true (exactly that value) restores the 0.4.0
    control flow: no state read, straight to the session. The state read adds one bounded
    control-plane call (about 2 x _MAX_API_WAIT_SECONDS worst case on an unreachable workspace
    with PAT auth; OAuth M2M keeps the databricks-sdk's own OIDC discovery budget on this call
    as on _connect(), see README Known limitations).

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
        # A ConfigError here lands in the same broad except it did when _connect() raised it.
        config = load_databricks_config()
        if databricks_warehouse_start_allowed():
            logger.info(
                "engine=databricks warehouse_state_guard=skipped reason=%s=true",
                DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR,
            )
        else:
            facts = _lookup_warehouse(config)
            if not _warehouse_is_running(facts):
                # Refused: the README-promised WARNING for every ok=false, and only that record
                # - the detail is server-authored, bounded and one line already, so it is
                # logged as-is and no second (INFO) record describes the same event.
                refusal = _not_verified_detail(config, facts)
                logger.warning("engine=databricks check_credentials_failed detail=%s", refusal)
                return CredentialCheckResult(engine="databricks", ok=False, detail=refusal)
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
        # redact -> present, the same chain as the failure path and the other engines.
        detail=(
            "Authenticated as "
            f"{present_remote_text(redact_secrets(str(current_user)), total_chars=len(str(current_user)))}."
        ),
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
    """What GET /api/2.0/sql/warehouses/{id} says about the http_path warehouse. `state` feeds
    the read-only warehouse-state guard (DECISIONS.md #20, audit record read-only-tools-
    auto-start-stopped-warehouse), which shares this one bounded call with the pricing."""

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

# Reported by explain_estimate whenever _lookup_warehouse returned None, with or without a
# dollar figure: it is the one sentence that tells the operator WHY the estimate is priced
# blind (and, for a fail-open configuration, the only diagnostic they get). The pricing clause
# that used to be glued to it lives in _rate_for and appears only beside a figure (PR #86
# round-2 review of f384634: the no-statistics branch dropped both together).
_UNOBSERVED_WAREHOUSE_CAVEAT = (
    "The configured SQL warehouse could not be observed (DATABRICKS_HTTP_PATH is not a "
    "/sql/1.0/warehouses/<id> path, or GET /api/2.0/sql/warehouses/<id> failed - e.g. the "
    "principal lacks CAN_VIEW/CAN_USE on it)."
)


# --- read-only warehouse-state guard (DECISIONS.md #20; issue #91) ----------------------------
#
# The Thrift session (sql.connect) is what starts a STOPPED Classic/Pro SQL warehouse; the only
# compute-side statements the read-only paths run (SELECT current_user(), EXPLAIN COST) sit
# behind it. So the guard stops sql.connect itself unless the Warehouses API reports RUNNING.
# The SDK State enum (databricks-sdk 0.148.0): DELETED, DELETING, RUNNING, STARTING, STOPPED,
# STOPPING - only RUNNING passes; the transitional STARTING refuses too (a session on it still
# bills). An unreadable state (lookup failed, non-warehouse http_path, no state field) is "not
# RUNNING": fail closed, the owner's decision in #91. This narrows #19's fail-open on the
# read-only paths for principals without CAN_VIEW/CAN_USE; #19's fallback survives for
# run_query_bounded (allow_warehouse_wake=True) and under the opt-in.
# Casing rule (shared with snowflake.py, DECISIONS.md #20): the state text is stripped and
# upper-cased before the compare, so a differently-cased running value passes and any other
# text refuses. The SDK enum is upper-case by construction - its parser turns an unknown or
# differently-cased wire value into None, i.e. "reports no state" - so the normalisation only
# matters for a non-SDK value; _WarehouseFacts.state keeps the value as returned, which is
# what the refusal texts present.
_RUNNING_STATE = "RUNNING"

_WAREHOUSE_START_HINT = (
    "Start the warehouse (or wait for it to finish starting) and retry, or set "
    f"{DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR}=true (exactly that value) to let "
    "check_credentials and estimate_query_cost open a session on a non-RUNNING warehouse, which "
    "starts it and bills compute until it auto-stops."
)
_WAREHOUSE_UNREADABLE_HINT = (
    "Fix DATABRICKS_HTTP_PATH or grant CAN_VIEW/CAN_USE on the warehouse, or set "
    f"{DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR}=true (exactly that value) to open the session "
    "regardless (which starts a stopped warehouse and bills compute)."
)
# The GET succeeded (so the path and the grant are fine) but the response carried no state
# field: neither remedy above applies, so this sub-case gets its own hint.
_WAREHOUSE_NO_STATE_HINT = (
    "The Warehouses API answered without a state field: retry, or check the warehouse in the "
    f"workspace UI; or set {DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR}=true (exactly that value) "
    "to open the session regardless (which starts a stopped warehouse and bills compute)."
)
_NO_EXPLAIN_RAN = (
    "No EXPLAIN COST was run: the byte and dollar estimates are unavailable, not zero."
)


def _present_observed(value: object) -> str:
    """A value that did not originate in this process - API-returned text, or an operator-
    configured identifier such as the warehouse id - on its way into a detail or a caveat:
    present_remote_text over its text (bounded, repr-quoted, one line - DECISIONS.md #18), the
    bare word None for a missing value so it does not read as the text 'None'. Same helper as
    snowflake._present_observed."""
    if value is None:
        return "None"
    text = str(value)
    return present_remote_text(redact_secrets(text), total_chars=len(text))


def _warehouse_is_running(facts: _WarehouseFacts | None) -> bool:
    return (
        facts is not None
        and facts.state is not None
        and facts.state.strip().upper() == _RUNNING_STATE
    )


def _not_running_sentence(warehouse_id: str, facts: _WarehouseFacts) -> str:
    # warehouse_id passed _WAREHOUSE_ID_RE (the lookup succeeded), but it is operator
    # configuration (the last DATABRICKS_HTTP_PATH segment) on its way to the MCP client and the
    # allowlist has no length bound, so it is presented like every other identifier - bounded,
    # repr-quoted; the state is API-returned text and is presented too. Never the hostname,
    # the http_path or an env value.
    return (
        f"The configured SQL warehouse {_present_observed(warehouse_id)} is "
        f"{_present_observed(facts.state)}, not {_RUNNING_STATE}, so no session was opened: "
        "opening one would start the warehouse and bill compute (fail closed)."
    )


def _state_unreadable_sentence(warehouse_id: str | None) -> str:
    """None: the lookup itself failed (no facts, so no id to name - the existing diagnostic
    sentence leads); otherwise the GET succeeded but the response carried no state field."""
    if warehouse_id is None:
        return (
            f"{_UNOBSERVED_WAREHOUSE_CAVEAT} Its state is therefore unknown and no session was "
            "opened (fail closed: a session on a non-RUNNING warehouse would start it and bill "
            "compute)."
        )
    return (
        f"The configured SQL warehouse {_present_observed(warehouse_id)} reports no state "
        "through the Warehouses API, so its state is unknown and no session was opened (fail "
        "closed: a session on a non-RUNNING warehouse would start it and bill compute)."
    )


def _guard_core_and_hint(
    config: DatabricksConfig, facts: _WarehouseFacts | None
) -> tuple[str, str]:
    """The (core sentence, remedy hint) pair for a refused read-only call."""
    if facts is None:
        return _state_unreadable_sentence(None), _WAREHOUSE_UNREADABLE_HINT
    # Cannot raise: facts is not None means _warehouse_id already accepted this path.
    warehouse_id = _warehouse_id(config.http_path)
    if facts.state is None:
        return _state_unreadable_sentence(warehouse_id), _WAREHOUSE_NO_STATE_HINT
    return _not_running_sentence(warehouse_id, facts), _WAREHOUSE_START_HINT


def _not_verified_detail(config: DatabricksConfig, facts: _WarehouseFacts | None) -> str:
    core, hint = _guard_core_and_hint(config, facts)
    return f"Not verified: {core} {hint}"


def _refused_estimate(config: DatabricksConfig, facts: _WarehouseFacts | None) -> CostEstimate:
    """The estimate_query_cost outcome when the guard refuses: no figures, exactly one caveat
    (no tier sentence, no pricing clause, no statistics sentence - none of those happened)."""
    core, hint = _guard_core_and_hint(config, facts)
    state = facts.state if facts is not None else None
    logger.info(
        "engine=databricks warehouse_state_guard=refused path=estimate_query_cost state=%s",
        _present_observed(state),
    )
    return CostEstimate(
        engine="databricks",
        accuracy_tier=AccuracyTier.HEURISTIC,
        estimated_bytes=None,
        estimated_cost_usd=None,
        caveats=[f"{core} {_NO_EXPLAIN_RAN} {hint}"],
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
    caller-asserted/default size so principals without CAN_VIEW/CAN_USE on the warehouse keep
    working (DECISIONS.md #19); its caveat here is only the pricing clause - the observation
    itself (_UNOBSERVED_WAREHOUSE_CAVEAT) is explain_estimate's to report in every outcome.
    """
    asserted_rate = dbus_per_hour(warehouse_size)
    if facts is None:
        caveat = (
            f"Priced at the caller-asserted {warehouse_size} size assuming a Serverless SQL "
            "warehouse, because the configured warehouse was not observed; the real cost is "
            "higher if the warehouse is larger, and Classic/Pro warehouses add a separate cloud "
            "VM cost not modeled here."
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
    # redact -> present, the same chain as every other warehouse-returned text (round-3
    # verification found this one caveat skipping the redaction step).
    presented_cluster_size = present_remote_text(
        redact_secrets(facts.cluster_size), total_chars=len(facts.cluster_size)
    )
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
    sql_text: str,
    warehouse: str | None,
    warehouse_size: str = "X-Small",
    *,
    allow_warehouse_wake: bool = False,
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

    The same lookup feeds the read-only warehouse-state guard (DECISIONS.md #20, issue #91):
    unless the observed state is RUNNING - also when it cannot be read - no session is opened
    and the estimate carries estimated_bytes=None, estimated_cost_usd=None and one caveat.
    `allow_warehouse_wake` (keyword-only, never a tool argument) is run_query_bounded's bypass:
    the execution that follows wakes the warehouse anyway. The operator opt-in
    COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true bypasses it for the read-only tool too.
    """
    # Validate the caller-asserted size before any lookup or connection: an unknown size is a
    # clear (sanitized) ValueError that never costs an API call.
    dbus_per_hour(warehouse_size)
    config = load_databricks_config()
    facts = _lookup_warehouse(config)
    env_opt_in = not allow_warehouse_wake and databricks_warehouse_start_allowed()
    if env_opt_in:
        logger.info(
            "engine=databricks warehouse_state_guard=skipped reason=%s=true",
            DATABRICKS_ALLOW_WAREHOUSE_START_ENV_VAR,
        )
    if not (allow_warehouse_wake or env_opt_in) and not _warehouse_is_running(facts):
        return _refused_estimate(config, facts)
    rate, size_label, rate_caveats = _rate_for(facts, warehouse_size)

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
            )
        ]
        # A dollar figure derived from no plan statistics would depend only on the
        # warehouse rate, not on the query (0.035 USD for every X-Small query) - a cap
        # compared against it looks enforced but never discriminates. Report it as
        # unavailable instead so run_query_bounded's fail-closed gate refuses a requested
        # max_estimated_cost_usd, exactly as it already does for the byte cap when
        # estimated_bytes is None (DECISIONS.md #5). The same None results when the observed
        # warehouse has no modelled rate (rate is None, see _rate_for).
        #
        # The caveat list describes the outcome actually returned (PR #86 verification, T4):
        # _rate_for's caveats say how the rate was chosen ("priced at the observed size",
        # "Priced at the caller-asserted ... size") when rate is not None, or why there is
        # none when it is. They are kept with a figure, or when they ARE the refusal; a
        # no-statistics estimate carries no pricing sentence, and a rate-None estimate no
        # runtime-baseline sentence, because neither describes anything that happened. The
        # observation that the warehouse could not be looked up is not a pricing sentence: it
        # happened in every outcome and is reported in every outcome.
        estimated_cost_usd: float | None = None
        if facts is None:
            caveats.append(_UNOBSERVED_WAREHOUSE_CAVEAT)
        if rate is None:
            caveats.extend(rate_caveats)
        if max_size_in_bytes is None:
            caveats.append(
                "EXPLAIN COST returned no size statistics for this query (common without "
                "ANALYZE TABLE having been run, for streaming sources, or for non-Delta "
                "external tables) - the byte and dollar estimates are unavailable, not zero; "
                "run_query_bounded will refuse any max_bytes_billed or max_estimated_cost_usd "
                "cap for this query."
            )
        elif rate is not None:
            runtime_hours = scale_runtime_hours(
                max_size_in_bytes, baseline_hours=_ASSUMED_RUNTIME_HOURS
            )
            estimated_cost_usd = round(rate * SERVERLESS_USD_PER_DBU * runtime_hours, 6)
            caveats.extend(rate_caveats)
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
    wrap_in_limit: bool = True,
    _max_wait_seconds: float | None = None,
) -> tuple[list[dict], int, bool]:
    """Execute `sql_text` with an optional row bound and a wall-clock execution cap.

    `warehouse` is accepted only for calling-convention consistency and is a documented
    no-op - see check_credentials's docstring for why.

    `wrap_in_limit` (#94): the tool passes whether the text is one SELECT/WITH/VALUES statement
    and so valid inside `SELECT * FROM (...) LIMIT max_rows + 1`; SHOW/DESCRIBE/EXPLAIN and the
    like run unwrapped and are capped by the paged fetch alone (result_bounds.collect_rows: the
    sentinel row or the running size estimate). The returned bool is True whenever that fetch
    stopped before the result was exhausted. The fetch runs under what is left of the
    wall-clock cap, checked before each page request that follows a full page.

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
        if max_rows is not None and wrap_in_limit:
            # `sql_text` is the caller's own query, passed as this tool's actual `sql`
            # parameter - wrapping their query in a LIMIT subquery to cap rows is this
            # function's job, not untrusted input reaching a query built elsewhere. The
            # trailing `;` and any trailing comments go first (sql_scope.single_statement_body,
            # shared with the other engines): either makes `(...) AS x LIMIT n` a syntax error.
            inner_sql = single_statement_body(sql_text, "databricks")
            wrapped_sql = f"SELECT * FROM ({inner_sql}) AS cost_guard_row_cap LIMIT {max_rows + 1}"  # noqa: S608

        # arraysize sizes the connector's own FetchResults round trips to the paged fetch below
        # (#94); its 100 MB buffer_size_bytes default stays as the byte bound per round trip.
        cur = conn.cursor(arraysize=FETCH_PAGE_SIZE)
        execution_error: list[BaseException] = []

        def _run() -> None:
            try:
                cur.execute(wrapped_sql)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
                execution_error.append(exc)

        # One wall-clock budget for the statement and its fetch: the fetch phase below takes
        # what the join leaves of it.
        deadline = time.monotonic() + max_wait_seconds
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

        def _check_fetch_deadline(fetched: int) -> None:
            # Only before a page request that follows a full page (#94): the first page is never
            # pre-empted, and a short page means the result is exhausted (fetchmany fills from
            # the server while has_more_rows), so the probe after it is never a download and a
            # complete result already in hand is never discarded. No cur.cancel(): the
            # statement finished, only its download overran.
            if time.monotonic() < deadline:
                return
            logger.warning(
                "engine=databricks execute_bounded result fetch exceeded %ss after %s rows",
                max_wait_seconds,
                fetched,
            )
            raise TimeoutError(
                f"Query exceeded {max_wait_seconds}s while fetching its result after "
                f"{fetched} rows."
            )

        # fetchall used to materialise the whole result here (#94): page it and stop at the
        # sentinel row or the size estimate instead.
        rows, row_cap_hit = collect_rows(
            (
                dict(zip(columns, row, strict=True))
                for row in iter_fetchmany(cur.fetchmany, between_pages=_check_fetch_deadline)
            ),
            max_rows,
        )
        cur.close()

        return rows, len(rows), row_cap_hit
    finally:
        # Outermost finally: runs on every exit path (success, timeout-raised, real-query-
        # error-reraised) after all existing cursor/cancel logic above. Best-effort, mirroring
        # cur.cancel()'s own discipline — a failed close must never mask the real outcome.
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
