"""cost-guard-mcp stdio server entry point.

IMPORTANT: never write to stdout in this process — stdout is the MCP transport
channel for stdio mode. All diagnostic output must go to stderr (the default
for Python's `logging` module when no handler is configured, and for `print(..., file=sys.stderr)`).
"""

from mcp.server import MCPServer

mcp = MCPServer("cost-guard-mcp")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
