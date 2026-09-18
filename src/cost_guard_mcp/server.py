"""cost-guard-mcp stdio server entry point.

IMPORTANT: never write to stdout in this process — stdout is the MCP transport
channel for stdio mode. All diagnostic output must go to stderr (the default
for Python's `logging` module when no handler is configured, and for `print(..., file=sys.stderr)`).
"""

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


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
@as_tool_error
def check_credentials(engine: Engine, warehouse: str | None = None) -> CredentialCheckResult:
    """Verify credentials/connectivity for an engine without running any real query or
    dry-run estimate. Call this once after configuring a new engine (or when a real tool
    call fails) to get a fast, clear yes/no signal instead of debugging via trial queries.
    """
    return _check_credentials(engine, warehouse)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
@as_tool_error
def describe_engine_capabilities(engine: Engine) -> EngineCapabilities:
    """Declare which cost signals are exact vs. approximate for the given warehouse engine.

    Call this before estimate_query_cost or run_query_bounded to understand how much to
    trust the accuracy_tier on their responses for this engine.
    """
    return _describe_engine_capabilities(engine)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
@as_tool_error
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

    warehouse_size (Snowflake/Databricks only, e.g. "SMALL", "X-Large") and edition
    (Snowflake only, e.g. "enterprise") default to the smallest/standard tier if omitted —
    set them to match the warehouse you actually run this query on, or the dollar figure
    will understate cost on a larger warehouse."""
    return _estimate_query_cost(engine, sql, warehouse, warehouse_size, edition)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True))
@as_tool_error
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

    warehouse_size/edition (see estimate_query_cost) default to the smallest/standard tier
    if omitted - set them to match the warehouse you actually run on, since max_estimated_cost_usd
    is checked against the estimate they produce."""
    # This tool is deliberately async so it can hand the actual blocking warehouse call to
    # anyio.to_thread.run_sync() itself with abandon_on_cancel=True, instead of relying on
    # the mcp SDK's own implicit sync-wrapping (which calls anyio.to_thread.run_sync() with
    # its default abandon_on_cancel=False and would shield this call from an MCP client's
    # `notifications/cancelled` until the underlying warehouse call finished on its own -
    # verified directly against the installed mcp SDK's
    # mcp.server.mcpserver.utilities.func_metadata.FuncMetadata.call_fn). abandon_on_cancel=True
    # makes cancellation prompt at the MCP bookkeeping level only - the abandoned thread (and
    # the warehouse-side query it's running) keeps going until the existing per-engine
    # watchdog (~120s) cancels it; see README's Known limitations.
    return await anyio.to_thread.run_sync(
        _run_query_bounded,
        engine,
        sql,
        max_bytes_billed,
        max_rows,
        max_estimated_cost_usd,
        warehouse,
        warehouse_size,
        edition,
        abandon_on_cancel=True,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
