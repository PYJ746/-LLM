"""Pet Hospital MCP — a stateless MCP server exposing the Go Pet Hospital REST API.

The service targets MCP protocol revision 2026-07-28 through the official
Python SDK 2.x `MCPServer`. It deliberately implements no `initialize`
handshake, no `Mcp-Session-Id`, no session store and no SSE resumption: every
request is a self-contained HTTP POST against the Streamable HTTP endpoint.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
