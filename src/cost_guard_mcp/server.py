"""cost-guard-mcp stdio server entry point.

IMPORTANT: never write to stdout in this process — stdout is the MCP transport
channel for stdio mode. All diagnostic output must go to stderr (the default
for Python's `logging` module when no handler is configured, and for `print(..., file=sys.stderr)`).
"""

import functools
import os

import anyio.from_thread
import anyio.to_thread
from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from cost_guard_mcp.errors import as_tool_error
from cost_guard_mcp.tools.check_credentials import check_credentials as _check_credentials
from cost_guard_mcp.tools.describe_engine_capabilities import (
    describe_engine_capabilities as _describe_engine_capabilities,
)
from cost_guard_mcp.tools.estimate_query_cost import (
    estimate_query_cost as _estimate_query_cost,
)
from cost_guard_mcp.tools.run_query_bounded import run_query_bounded as _run_query_bounded
from cost_guard_mcp.types import (
    BoundedQueryResult,
    CostEstimate,
    CredentialCheckResult,
    Engine,
    EngineCapabilities,
)

mcp = MCPServer("cost-guard-mcp")

# Registration shape matters beyond Python: the mcprush.com marketplace reads this
# project's tool list statically from the repository (its "Read the tools again" control),
# and that reader only recognises a single-line `@mcp.tool(...)` placed directly above the
# `def` - the previous multi-line decorator with `@as_tool_error` stacked under it came
# back as "found no tool declarations" (2026-10-10). So the annotations live in constants
# and as_tool_error wraps the implementation functions instead; the registered tools, their
# signatures and docstrings are unchanged (tools/list is byte-identical).
_CHECK_CREDENTIALS_ANNOTATIONS = ToolAnnotations(
    title="Check Credentials", read_only_hint=True, open_world_hint=True
)
_DESCRIBE_ENGINE_CAPABILITIES_ANNOTATIONS = ToolAnnotations(
    title="Describe Engine Capabilities", read_only_hint=True, open_world_hint=True
)
_ESTIMATE_QUERY_COST_ANNOTATIONS = ToolAnnotations(
    title="Estimate Query Cost", read_only_hint=True, open_world_hint=True
)
_RUN_QUERY_BOUNDED_ANNOTATIONS = ToolAnnotations(
    title="Run Query Bounded", read_only_hint=False, open_world_hint=True
)

_check_credentials_safe = as_tool_error(_check_credentials)
_describe_engine_capabilities_safe = as_tool_error(_describe_engine_capabilities)
_estimate_query_cost_safe = as_tool_error(_estimate_query_cost)


async def _run_query_bounded_in_thread(
    engine: Engine,
    sql: str,
    max_bytes_billed: int | None,
    max_rows: int | None,
    max_estimated_cost_usd: float | None,
    warehouse: str | None,
    warehouse_size: str | None,
    edition: str | None,
) -> BoundedQueryResult:
    # Deliberately async so it can hand the actual blocking warehouse call to
    # anyio.to_thread.run_sync() itself with abandon_on_cancel=True, instead of relying on
    # the mcp SDK's own implicit sync-wrapping (which calls anyio.to_thread.run_sync() with
    # its default abandon_on_cancel=False and would shield this call from an MCP client's
    # `notifications/cancelled` until the underlying warehouse call finished on its own -
    # verified directly against the installed mcp SDK's
    # mcp.server.mcpserver.utilities.func_metadata.FuncMetadata.call_fn). abandon_on_cancel=True
    # makes cancellation prompt at the MCP bookkeeping level only - the abandoned thread (and
    # the warehouse-side query it's running) keeps going until the existing per-engine
    # watchdog (~120s) cancels it; see README's Known limitations and DECISIONS.md #14.
    #
    # before_execute=anyio.from_thread.check_cancelled is the cooperative half of that
    # trade-off: the tool body calls it once, after the estimate and every cap check and
    # immediately before the execution is dispatched, so a cancel that arrived during the
    # estimate raises inside the (already abandoned) worker thread instead of letting it
    # submit a billable query into a cancelled future nobody reads. It inspects the thread's
    # anyio cancel-scope chain - the mcp dispatcher cancels the handler's CancelScope
    # (peer_cancel_mode "interrupt"), which is what trips it; a bare asyncio Task.cancel()
    # would not. Bound here, not in the tool body: check_cancelled raises NoEventLoopError
    # when called outside an anyio worker thread.
    return await anyio.to_thread.run_sync(
        functools.partial(
            _run_query_bounded,
            engine,
            sql,
            max_bytes_billed,
            max_rows,
            max_estimated_cost_usd,
            warehouse,
            warehouse_size,
            edition,
            before_execute=anyio.from_thread.check_cancelled,
        ),
        abandon_on_cancel=True,
    )


# Wrapping the async helper (not the sync implementation) keeps as_tool_error's async code
# path, exactly as when it was stacked on the async tool itself. The helper is renamed first
# so the per-call outcome record (functools.wraps copies __name__ at wrap time) reads
# `tool=run_query_bounded` - the registered tool - rather than naming this thread helper;
# nothing else reads that __name__.
_run_query_bounded_in_thread.__name__ = "run_query_bounded"
_run_query_bounded_safe = as_tool_error(_run_query_bounded_in_thread)


@mcp.tool(annotations=_CHECK_CREDENTIALS_ANNOTATIONS)
def check_credentials(engine: Engine, warehouse: str | None = None) -> CredentialCheckResult:
    """Verify credentials/connectivity for an engine without running any real query or
    dry-run estimate. Call this once after configuring a new engine (or when a real tool
    call fails) to get a fast, clear yes/no signal instead of debugging via trial queries.
    """
    return _check_credentials_safe(engine, warehouse)


@mcp.tool(annotations=_DESCRIBE_ENGINE_CAPABILITIES_ANNOTATIONS)
def describe_engine_capabilities(engine: Engine) -> EngineCapabilities:
    """Declare which cost signals are exact vs. approximate for the given warehouse engine.

    Call this before estimate_query_cost or run_query_bounded to understand how much to
    trust the accuracy_tier on their responses for this engine.
    """
    return _describe_engine_capabilities_safe(engine)


@mcp.tool(annotations=_ESTIMATE_QUERY_COST_ANNOTATIONS)
def estimate_query_cost(
    engine: Engine,
    sql: str,
    warehouse: str | None = None,
    warehouse_size: str | None = None,
    edition: str | None = None,
) -> CostEstimate:
    """Estimate the cost of a SQL query before running it. ALWAYS call this before running
    an expensive-looking query. The response's accuracy_tier tells you how much to trust
    the number: PRECISE (exact), UPPER_BOUND (real cap, may overstate), HEURISTIC (rough).
    Accepts exactly one SQL statement per call (a single trailing ';' is fine) - text with
    more statements is refused before anything is estimated.

    warehouse_size (Snowflake/Databricks only, e.g. "SMALL", "X-Large") and edition
    (Snowflake only, e.g. "enterprise") default to the smallest/standard tier if omitted —
    set them to match the warehouse you actually run this query on, or the dollar figure
    will understate cost on a larger warehouse."""
    return _estimate_query_cost_safe(engine, sql, warehouse, warehouse_size, edition)


@mcp.tool(annotations=_RUN_QUERY_BOUNDED_ANNOTATIONS)
async def run_query_bounded(
    engine: Engine,
    sql: str,
    max_bytes_billed: int | None = None,
    max_rows: int | None = None,
    max_estimated_cost_usd: float | None = None,
    warehouse: str | None = None,
    warehouse_size: str | None = None,
    edition: str | None = None,
) -> BoundedQueryResult:
    """Run a query only if its pre-flight cost estimate is within your given bounds; refuses
    otherwise (check result.status — "refused" means it did NOT run and result.hint explains
    why). NOTE: unlike estimate_query_cost, a successful call here has a real monetary/quota
    side effect — don't call this repeatedly without inspecting the result of each call.
    Accepts exactly one SQL statement per call (a single trailing ';' is fine) - text with
    more statements is refused before anything is estimated or run.

    warehouse_size/edition (see estimate_query_cost) default to the smallest/standard tier
    if omitted - set them to match the warehouse you actually run on, since max_estimated_cost_usd
    is checked against the estimate they produce."""
    return await _run_query_bounded_safe(
        engine,
        sql,
        max_bytes_billed,
        max_rows,
        max_estimated_cost_usd,
        warehouse,
        warehouse_size,
        edition,
    )


# The standard OTel SDK env var (see
# https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/) - reused as the
# single opt-in gate below, rather than inventing a cost-guard-mcp-specific variable name
# a real OTel Collector deployment wouldn't already be setting.
_OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"


def _bootstrap_opentelemetry() -> None:
    """Register a real TracerProvider + OTLP exporter as the global tracer provider, but
    only if OTEL_EXPORTER_OTLP_ENDPOINT is set. A no-op otherwise.

    The `mcp` SDK ships an `OpenTelemetryMiddleware` on by default for every server built
    this way (see `mcp.server.lowlevel.server.Server.__init__`) - it wraps every inbound
    message in a SERVER span unconditionally, but that middleware is a documented no-op
    until a real exporter is registered as the global tracer provider; this project ships
    with none configured, so without this function every one of those spans is silently
    dropped. `opentelemetry-sdk` and the OTLP exporter are optional dependencies (the
    `otel` extra, see pyproject.toml) - imported here, and nowhere else in this project, so
    a deployment that never sets the env var never needs them installed at all.
    """
    endpoint = os.environ.get(_OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR)
    if not endpoint:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: mcp.name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)


def main() -> None:
    _bootstrap_opentelemetry()
    mcp.run()


if __name__ == "__main__":
    main()
