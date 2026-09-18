"""The approval queue.

A policy decision of `approve` is not an allow. It parks the call, tells a human,
and waits. If nobody answers within the timeout, the answer is **deny** - an
unattended write to patient data is exactly the case where silence must not mean
yes.

The console runs on a different event loop in a different thread, so a
resolution arriving from a button click has to be handed back to the loop that
is waiting. That is what `call_soon_threadsafe` is doing here.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from governance import settings
from governance.events import bus

Outcome = Literal["approved", "rejected", "timeout"]

TIMEOUT_REASON = (
    "nobody answered within {seconds:.0f}s, and an unanswered request to write "
    "regulated data is a denial"
)


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    agent: str
    kind: str
    target: str
    arguments: dict[str, Any]
    classification: str | None
    rule_id: str
    reason: str
    requested_at: str
    timeout_seconds: float
    audit_id: int | None = None
    delegation_chain: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "kind": self.kind,
            "target": self.target,
            "arguments": self.arguments,
            "classification": self.classification,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "requested_at": self.requested_at,
            "timeout_seconds": self.timeout_seconds,
            "delegation_chain": list(self.delegation_chain),
        }


@dataclass(frozen=True)
class Resolution:
    outcome: Outcome
    resolved_by: str
    reason: str

    @property
    def approved(self) -> bool:
        return self.outcome == "approved"


@dataclass
class _Pending:
    request: ApprovalRequest
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future
    handle: Any = field(default=None)


class ApprovalQueue:
    """Process-wide. The act, the console and the tests all see one queue."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        # Set in non-interactive runs (`demo rehearse`). Given the request, it
        # returns the resolution to apply immediately.
        self.auto_resolver: Callable[[ApprovalRequest], Resolution] | None = None

    # -- the waiting side --------------------------------------------------

    async def request(
        self,
        *,
        agent: str,
        kind: str,
        target: str,
        arguments: dict[str, Any] | None = None,
        classification: str | None = None,
        rule_id: str = "",
        reason: str = "",
        timeout_seconds: float | None = None,
        audit_id: int | None = None,
        delegation_chain: tuple[str, ...] = (),
    ) -> tuple[ApprovalRequest, Resolution]:
        timeout = timeout_seconds if timeout_seconds is not None \
            else settings.approval_timeout_seconds()
        request = ApprovalRequest(
            id=uuid.uuid4().hex[:12],
            agent=agent,
            kind=kind,
            target=target,
            arguments=dict(arguments or {}),
            classification=classification,
            rule_id=rule_id,
            reason=reason,
            requested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            timeout_seconds=timeout,
            audit_id=audit_id,
            delegation_chain=delegation_chain,
        )

        if self.auto_resolver is not None:
            resolution = self.auto_resolver(request)
            bus().publish("approval.resolved", request=request.as_dict(),
                          outcome=resolution.outcome, resolved_by=resolution.resolved_by)
            return request, resolution

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        with self._lock:
            self._pending[request.id] = _Pending(request, loop, future)
        bus().publish("approval.requested", request=request.as_dict())

        try:
            resolution: Resolution = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            resolution = Resolution(
                outcome="timeout",
                resolved_by="nobody",
                reason=TIMEOUT_REASON.format(seconds=timeout),
            )
        finally:
            with self._lock:
                self._pending.pop(request.id, None)

        bus().publish("approval.resolved", request=request.as_dict(),
                      outcome=resolution.outcome, resolved_by=resolution.resolved_by)
        return request, resolution

    # -- the answering side ------------------------------------------------

    def resolve(self, request_id: str, *, approved: bool, resolved_by: str) -> bool:
        """Answer a parked request. Safe to call from any thread or loop."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        resolution = Resolution(
            outcome="approved" if approved else "rejected",
            resolved_by=resolved_by,
            reason=(f"approved by {resolved_by}" if approved
                    else f"rejected by {resolved_by}"),
        )

        def _set() -> None:
            if not pending.future.done():
                pending.future.set_result(resolution)

        try:
            pending.loop.call_soon_threadsafe(_set)
        except RuntimeError:  # pragma: no cover - the waiter's loop already went away
            return False
        return True

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            return [p.request for p in self._pending.values()]

    def clear(self) -> None:
        with self._lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            with_result = Resolution("timeout", "nobody", "the queue was cleared")
            try:
                pending.loop.call_soon_threadsafe(
                    lambda p=pending, r=with_result: (
                        None if p.future.done() else p.future.set_result(r)
                    )
                )
            except RuntimeError:  # pragma: no cover
                pass


_QUEUE = ApprovalQueue()


def approval_queue() -> ApprovalQueue:
    return _QUEUE


def auto_approve(request: ApprovalRequest) -> Resolution:
    """Used by `demo rehearse`, never on stage."""
    return Resolution("approved", "rehearsal (auto)", "auto-approved by the rehearsal harness")


def auto_reject(request: ApprovalRequest) -> Resolution:
    return Resolution("rejected", "rehearsal (auto)", "auto-rejected by the rehearsal harness")
