"""cost-guard-mcp stdio server entry point.

IMPORTANT: never write to stdout in this process — stdout is the MCP transport
channel for stdio mode. All diagnostic output must go to stderr (the default
for Python's `logging` module when no handler is configured, and for `print(..., file=sys.stderr)`).
"""

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from cost_guard_mcp.tools.describe_engine_capabilities import (
    describe_engine_capabilities as _describe_engine_capabilities,
)
from cost_guard_mcp.tools.estimate_query_cost import (
    estimate_query_cost as _estimate_query_cost,
)
from cost_guard_mcp.types import CostEstimate, Engine, EngineCapabilities

mcp = MCPServer("cost-guard-mcp")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
def describe_engine_capabilities(engine: Engine) -> EngineCapabilities:
    """Declare which cost signals are exact vs. approximate for the given warehouse engine.

    Call this before estimate_query_cost or run_query_bounded to understand how much to
    trust the accuracy_tier on their responses for this engine.
    """
    return _describe_engine_capabilities(engine)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
def estimate_query_cost(engine: Engine, sql: str, warehouse: str | None = None) -> CostEstimate:
    """Estimate the cost of a SQL query before running it. ALWAYS call this before running
    an expensive-looking query. The response's accuracy_tier tells you how much to trust
    the number: PRECISE (exact), UPPER_BOUND (real cap, may overstate), HEURISTIC (rough)."""
    return _estimate_query_cost(engine, sql, warehouse)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
