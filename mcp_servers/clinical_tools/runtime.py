"""Run the `clinical-tools` MCP server in-process over streamable HTTP.

The demo needs a real MCP server so that the toolbox, the middleware and the
trace really do cross an MCP boundary - Agent Framework propagates trace context
to client-opened MCP sessions, so act 4 gets a genuine distributed trace. But
the demo must also be offline and start in seconds on stage, so the server runs
inside the act process on an ephemeral loopback port rather than as a container
or a separate shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator

import uvicorn

from .server import mcp, reset_state


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def clinical_tools_server(port: int | None = None) -> AsyncIterator[str]:
    """Start the MCP server and yield its streamable-HTTP endpoint URL."""
    reset_state()
    port = port or _free_port()
    config = uvicorn.Config(
        mcp.streamable_http_app(),
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        # uvicorn flips `started` once the socket is accepting connections.
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("clinical-tools MCP server did not start in time")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
