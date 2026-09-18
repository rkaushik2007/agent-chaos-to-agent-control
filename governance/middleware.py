"""Enforcement engine 1: Agent Framework middleware. The default.

Two pieces:

* `GovernanceAgentMiddleware` puts the acting `Principal` in scope for the whole
  run, so the function middleware can answer "who is calling?".
* `GovernanceFunctionMiddleware` runs before every tool invocation, calls the
  shared seam, and on a refusal sets `context.result` and does **not** call
  `call_next()`. The tool is never invoked.

Short-circuiting by not calling `call_next()` is the documented Agent Framework
mechanism, and it matters that the denial comes back as a tool *result* rather
than an exception: the model sees a clear refusal, can tell the user why, and
the run continues instead of collapsing. `MiddlewareFailure` is reserved for the
case where the governance layer itself could not decide - see below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework import (
    AgentContext,
    AgentMiddleware,
    FunctionInvocationContext,
    FunctionMiddleware,
    MiddlewareFailure,
)

from governance.context import acting_as, current_principal
from governance.enforce import GuardResult, guard_tool
from governance.identity import Principal


def arguments_as_dict(arguments: Any) -> dict[str, Any]:
    """`FunctionInvocationContext.arguments` is a model or a mapping."""
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return dict(arguments)
    for attr in ("model_dump", "dict"):
        dump = getattr(arguments, attr, None)
        if callable(dump):
            try:
                return dict(dump())
            except Exception:  # pragma: no cover - defensive
                break
    try:
        return dict(arguments)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {"value": repr(arguments)}


class GovernanceAgentMiddleware(AgentMiddleware):
    """Establish who is acting for the duration of an agent run."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    async def process(
        self,
        context: AgentContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        with acting_as(self.principal):
            await call_next()


class GovernanceFunctionMiddleware(FunctionMiddleware):
    """Evaluate policy before every tool call."""

    def __init__(
        self,
        principal: Principal | None = None,
        *,
        on_decision: Callable[[GuardResult], None] | None = None,
    ) -> None:
        # An explicit principal is preferred when one middleware instance serves
        # exactly one agent run, which is how the acts build them. The context
        # variable is the fallback for callers that are not going through an
        # `Agent` at all - the A2A gateway, and the tests.
        self.principal = principal
        self.on_decision = on_decision
        self.decisions: list[GuardResult] = []

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        principal = self.principal or current_principal()
        if principal is None:
            # Fail closed, and loudly. A tool call the layer cannot attribute is
            # not a tool call it may quietly allow. `MiddlewareFailure` aborts
            # the run rather than being converted into a tool error the model
            # could work around.
            raise MiddlewareFailure(
                "No principal in scope for "
                f"{context.function.name}. Every governed run must execute inside "
                "`acting_as(...)`; refusing to guess who is calling."
            )

        try:
            result = await guard_tool(
                principal, context.function.name, arguments_as_dict(context.arguments)
            )
        except Exception as exc:
            # Agent Framework turns an ordinary middleware exception into a tool
            # error and lets the loop continue. For a bug inside the governance
            # layer that is fail-open in spirit: the layer did not decide, and
            # the run carries on regardless. `MiddlewareFailure` aborts the run
            # instead, which is the only safe reading of "the guard broke".
            raise MiddlewareFailure(
                f"Helix governance could not evaluate {context.function.name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        self.decisions.append(result)
        if self.on_decision is not None:
            self.on_decision(result)

        if not result.allowed:
            # Not calling `call_next()` is what stops the tool. The agent gets
            # the refusal as the function's result.
            context.result = result.message
            return

        await call_next()
