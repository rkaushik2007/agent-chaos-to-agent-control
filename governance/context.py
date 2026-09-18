"""Who is acting, right now, on this task.

Agent Framework's function middleware receives the tool call but not the agent
that caused it, and the Agent Hooks contract hands its interceptors a context
mapping of its own shape. Rather than teach the governance seam two different
ways to answer "who is calling?", the agent runner puts the `Principal` in a
context variable and everything downstream reads it from here.

A `ContextVar` is the right tool: it is per-task, so concurrent agent runs in
one process never see each other's principal, and it needs no framework, so the
MOCK executor, the middleware and the hooks interceptor all use the same one.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

from governance.identity import Principal

_PRINCIPAL: ContextVar[Principal | None] = ContextVar("helix_principal", default=None)
_DEPTH: ContextVar[int] = ContextVar("helix_delegation_depth", default=0)
# The chain of agents that led here, oldest first. Carried across the A2A hop so
# a denial can name the whole path, not just the agent that tripped it.
_CHAIN: ContextVar[tuple[str, ...]] = ContextVar("helix_delegation_chain", default=())


def current_principal() -> Principal | None:
    return _PRINCIPAL.get()


def require_principal() -> Principal:
    principal = _PRINCIPAL.get()
    if principal is None:
        raise RuntimeError(
            "No principal is in scope. Every governed call runs inside "
            "`acting_as(...)`; a call that does not is a call nobody can attribute."
        )
    return principal


def delegation_depth() -> int:
    return _DEPTH.get()


def delegation_chain() -> tuple[str, ...]:
    return _CHAIN.get()


@contextlib.contextmanager
def acting_as(
    principal: Principal,
    *,
    depth: int | None = None,
    chain: tuple[str, ...] | None = None,
) -> Iterator[Principal]:
    """Run a block as this principal.

    `depth` and `chain` are set when entering a delegated hop, so the callee
    knows how far from the originating request it is. Both restore on exit.
    """
    tokens = [
        (_PRINCIPAL, _PRINCIPAL.set(principal)),
        (_DEPTH, _DEPTH.set(_DEPTH.get() if depth is None else depth)),
        (
            _CHAIN,
            _CHAIN.set(
                (*_CHAIN.get(), principal.agent_id) if chain is None else chain
            ),
        ),
    ]
    try:
        yield principal
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def unattributed() -> contextlib.AbstractContextManager[None]:
    """Explicitly clear the principal.

    Act 1 uses this: the calls really are unattributed, and pretending otherwise
    by leaving a stale principal in scope would make the chaos look tidier than
    it is.
    """

    @contextlib.contextmanager
    def _cm() -> Iterator[None]:
        token = _PRINCIPAL.set(None)
        try:
            yield
        finally:
            _PRINCIPAL.reset(token)

    return _cm()
