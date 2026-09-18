"""Run an ASGI app on loopback for the duration of a block.

Each server gets **its own thread and its own event loop**. That is not
over-engineering; it is the fix for a concrete failure.

The MCP SDK's `StreamableHTTPSessionManager` leaves state behind in the event
loop it ran on. Start a third streamable-HTTP MCP server in one event loop - as
a rehearsal that runs four acts back to back does, and as the test suite does -
and it accepts the connection, starts the response, then returns without
finishing it. The client sees "peer closed connection without sending complete
message body" and hangs until its timeout; the server logs "ASGI callable
returned without completing response". Two servers per loop is fine. The third
is not. A plain Starlette app on this same helper survives indefinitely, and
running each cycle under its own `asyncio.run` also survives indefinitely, which
is what pins the problem to per-loop state rather than to ports, sockets or
shutdown ordering.

Giving every server a fresh loop sidesteps it completely, and is closer to the
truth anyway: these are separate services that happen to live in one process.

Because the server runs on another loop, anything it needs to await - an
upstream MCP connection, for example - must be created inside that loop. See the
toolbox aggregator's lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import AsyncIterator

import uvicorn

START_TIMEOUT = 15.0
SHUTDOWN_TIMEOUT = 10.0


def bind_loopback() -> socket.socket:
    """A bound, listening loopback socket on a port the OS chose.

    `SO_REUSEADDR` is deliberately NOT set. On Windows it does not mean what it
    means on Linux: it lets a second socket bind an address another socket is
    still listening on, and connections are then delivered to either one. Every
    server here asks for port 0, so we never need to re-bind a fixed port, and
    leaving the option off makes the OS hand out a port nothing else holds.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


@contextlib.asynccontextmanager
async def serve_asgi(app=None, *, app_factory=None) -> AsyncIterator[str]:
    """Serve an ASGI app on loopback in a background thread, yielding its base URL.

    Pass `app_factory` instead of `app` for a service that must embed its own
    address in what it serves - an A2A agent card, for instance. The socket is
    bound first, so the factory receives a URL that is already real and there is
    no second bind to race with. ASGI apps are themselves callable, so this is an
    explicit argument rather than a guess about what was passed.
    """
    if (app is None) == (app_factory is None):
        raise TypeError("serve_asgi takes exactly one of `app` or `app_factory`")

    sock = bind_loopback()
    host, port = sock.getsockname()[:2]
    if app_factory is not None:
        app = app_factory(f"http://{host}:{port}")

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            # Without a bound here uvicorn waits indefinitely for idle keep-alive
            # connections to close, and an MCP client that has been closed but
            # whose pooled socket lingers adds seconds to every act and every
            # test. One second is plenty for a loopback demo.
            timeout_graceful_shutdown=1,
        )
    )
    failure: list[BaseException] = []
    finished = threading.Event()

    def _run() -> None:
        # uvicorn skips signal capture off the main thread, so it will not touch
        # this process's Ctrl-C handling.
        try:
            asyncio.run(server.serve(sockets=[sock]))
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller below
            failure.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=_run, name=f"asgi-{port}", daemon=True)
    thread.start()

    deadline = asyncio.get_running_loop().time() + START_TIMEOUT
    while not server.started:
        if failure:
            raise failure[0]
        if finished.is_set():
            raise RuntimeError(f"server on {host}:{port} exited before it started")
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError(f"server on {host}:{port} did not start in time")
        await asyncio.sleep(0.01)

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, SHUTDOWN_TIMEOUT)
        if thread.is_alive():
            # Nothing safe is left to do to another thread's loop; the socket is
            # closed below so the port is not quietly served by a zombie.
            server.force_exit = True
            await asyncio.to_thread(thread.join, SHUTDOWN_TIMEOUT)
        with contextlib.suppress(OSError):
            sock.close()
