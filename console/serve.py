"""Run the console alongside an act.

Act 3 needs somebody to press Approve while the act is waiting, so the console
runs inside the act's own process. That keeps one approval queue and one audit
store, and means there is nothing to start in the right order on stage.

`uv run demo console` serves the same app standalone for browsing afterwards.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from hosting import serve_asgi


DEFAULT_PORT = 8000


@contextlib.asynccontextmanager
async def console_server(port: int = DEFAULT_PORT) -> AsyncIterator[str]:
    """Serve the console on loopback for the duration of the block.

    Port 8000 by default so the runbook can print the URL before the talk. If
    something already holds it the OS picks another port rather than failing the
    act - a console on the wrong port is recoverable, an act that will not start
    is not.
    """
    from console.app import app

    async with serve_asgi(app, port=port) as base_url:
        yield base_url
