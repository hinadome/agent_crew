"""A tiny but REAL MCP server (stdio transport) used by the integration tests."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo-test")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def shout(text: str) -> str:
    """Upper-case some text."""
    return text.upper()


if __name__ == "__main__":
    server.run("stdio")
