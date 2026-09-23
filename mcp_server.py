"""MCP server: exposes the same tools the web app uses, so Claude Desktop / any MCP client can call them.

Run (stdio):  python mcp_server.py
Claude Desktop config:
  {"mcpServers": {"sarkari-sahayak": {"command": "python", "args": ["/full/path/to/mcp_server.py"]}}}
"""
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

import engine

mcp = _Server("sarkari-sahayak")
for _fn in engine.TOOLS.values():  # one registry, shared with server.py
    mcp.tool()(_fn)

if __name__ == "__main__":
    mcp.run()
