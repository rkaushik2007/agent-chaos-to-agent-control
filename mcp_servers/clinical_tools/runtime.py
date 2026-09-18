"""Run the `clinical-tools` MCP server in-process over streamable HTTP.

The demo needs a real MCP server so that the toolbox, the middleware and the
trace really do cross an MCP boundary - Agent Framework propagates trace context
to client-opened MCP sessions, so act 4 gets a genuine distributed trace. But
the demo must also be offline and start in seconds on stage, so the server runs
inside the act process on an ephemeral loopback port rather than as a container
or a separate shell.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from hosting import serve_asgi

from .server import build_server, reset_state


@contextlib.asynccontextmanager
async def clinical_tools_server() -> AsyncIterator[str]:
    """Start the MCP server and yield its streamable-HTTP endpoint URL."""
    reset_state()
    async with serve_asgi(build_server().streamable_http_app()) as base_url:
        yield f"{base_url}/mcp"
