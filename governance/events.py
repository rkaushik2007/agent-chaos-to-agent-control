"""A tiny in-process event bus for the console's live feed.

The console runs on its own event loop in its own thread (see
`hosting.serve_asgi`), while decisions are made on the act's loop. Publishing
therefore has to cross loops, which is why every subscriber records the loop it
subscribed from and gets its items via `call_soon_threadsafe`.

Deliberately not a queue with delivery guarantees. If nobody is watching, events
are dropped; the audit store is the record that matters, and this only drives a
panel on a screen.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

MAX_BACKLOG = 200

# Pushed to every subscriber to end its generator cleanly. Without it the only
# thing that ever stops an SSE stream is the server cancelling it mid-response,
# and sse-starlette re-raises that cancellation out of its own task group where
# no amount of guarding inside the generator can catch it.
_CLOSED = object()


@dataclass(frozen=True)
class Event:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def publish(self, event_kind: str, /, **payload: Any) -> Event:
        # `event_kind` is positional-only: payloads legitimately carry their own
        # `kind` (a decision's target kind is "tool" or "a2a"), and without this
        # the two collide at the call site.
        event = Event(kind=event_kind, payload=payload)
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, event)
            except RuntimeError:
                # The subscriber's loop has closed; it will be dropped when its
                # generator next unwinds.
                continue
        return event

    async def subscribe(self) -> AsyncIterator[Event]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_BACKLOG)
        entry = (loop, queue)
        with self._lock:
            self._subscribers.append(entry)
        try:
            while True:
                item = await queue.get()
                if item is _CLOSED:
                    return
                yield item
        except (asyncio.CancelledError, GeneratorExit):
            return
        finally:
            with self._lock:
                if entry in self._subscribers:
                    self._subscribers.remove(entry)

    def close(self) -> None:
        """End every subscriber's stream.

        Called before the console's server stops, so in-flight SSE responses
        finish on their own terms instead of being cancelled underneath
        sse-starlette.
        """
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, _CLOSED)
            except RuntimeError:
                continue

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def _offer(queue: asyncio.Queue, event: Event) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # A viewer that has fallen 200 events behind is not reading anyway.
        with_suppress = queue.get_nowait
        try:
            with_suppress()
            queue.put_nowait(event)
        except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
            pass


_BUS = EventBus()


def bus() -> EventBus:
    return _BUS
