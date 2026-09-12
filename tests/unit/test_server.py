import asyncio

from cost_guard_mcp.server import mcp


def test_server_is_named_cost_guard_mcp():
    assert mcp.name == "cost-guard-mcp"


def test_server_lists_no_tools_before_any_are_registered_elsewhere():
    # This test documents current state; it's updated in Task 1.2 once a tool is added.
    tools = asyncio.get_event_loop().run_until_complete(mcp.list_tools())
    assert isinstance(tools, list)
