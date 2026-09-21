"""Run the console alongside an act.

Act 3 needs somebody to press Approve while the act is waiting, so the console
runs inside the act's own process. That keeps one approval queue and one audit
store, and means there is nothing to start in the right order on stage.

`uv run demo console` serves the same app standalone for browsing afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from hosting import serve_asgi


async def _another_console_is_running(port: int) -> bool:
    """Is a *different* copy of this console already on that port?

    Worth knowing, because the approval queue is per-process. A console started
    with `demo console` has its own empty queue, so pressing Approve there does
    nothing for a waiting act - it just sits there until it times out and
    denies. That is a silent, confusing failure at exactly the wrong moment.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=0.5) as client:
            response = await client.get(f"http://127.0.0.1:{port}/api/health")
        payload = response.json()
    except Exception:
        return False
    return isinstance(payload, dict) and "pending_approvals" in payload


@contextlib.asynccontextmanager
async def console_server(port: int | None = None) -> AsyncIterator[str]:
    """Serve the console on loopback for the duration of the block.

    A fixed port by default so the runbook can print the URL before the talk -
    see `governance.settings.DEFAULT_CONSOLE_PORT` for why it is not 8000.
    `CONSOLE_PORT` overrides it.

    If something already holds the port the OS picks another rather than failing
    the act: a console on a different port is recoverable, an act that will not
    start is not. But it says so, loudly, because a runbook that prints one URL
    while the console is on another is worse than no runbook.
    """
    from console.app import app
    from governance import settings

    preferred = settings.console_port() if port is None else port

    from governance.events import bus

    async with serve_asgi(app, port=preferred) as base_url:
        actual = int(base_url.rsplit(":", 1)[1])
        if preferred and actual != preferred:
            from scripts import narrate

            narrate.warn(
                f"port {preferred} is taken by something else, so the console is "
                f"on {actual} instead. Use the URL below, not the one in the runbook."
            )
            if await _another_console_is_running(preferred):
                narrate.error(
                    f"and the thing on {preferred} is another copy of this console. "
                    "Approvals clicked there will NOT reach this act - the queue "
                    "lives in the act's own process. Close it and use the URL below."
                )
        try:
            yield base_url
        finally:
            # Let open SSE streams end themselves before uvicorn starts
            # cancelling in-flight responses.
            bus().close()
            await asyncio.sleep(0.15)
