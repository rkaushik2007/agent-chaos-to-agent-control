"""Enforcement engine 2: Agent Hooks.

Agent Framework ships a first-class governance control plane that implements the
framework-neutral AGENT-HOOKS-0.1 contract: interceptors receive a context at
defined interception points and return a verdict, the runtime fails closed, and
every verdict produces a payload-free interception record.

    https://learn.microsoft.com/en-us/agent-framework/agents/agent-hooks

That is the same shape as this demo's governance layer, so the demo implements
the contract rather than competing with it. Select it with
`GOVERNANCE_ENGINE=hooks`.

It is **not** the default. The SDK is experimental (`agent-hooks-sdk` 0.1.0a5)
and the stage default should be the GA middleware API.

One deliberate choice worth explaining out loud: an `approve` decision does not
return `Verdict.escalate(...)` for the bundle's approval resolver to lift.
Instead, the interceptor calls the same `enforce.apply_decision` the middleware
engine calls, which parks the request on the same approval queue and writes the
same audit row. Using the resolver seam would give one engine a different audit
path from the other, and the entire claim of this session is that the governance
layer does not change when the plumbing does. The contract is honoured where it
is observable - real verdicts at real interception points, fail-closed, with
records flowing to the console.
"""

from __future__ import annotations

from typing import Any

from agent_hooks import ALLOW, AgentContext, Verdict

from governance.context import current_principal
from governance.enforce import GuardResult, guard_tool
from governance.identity import Principal

PRE_TOOL_CALL = "pre_tool_call"


class GovernancePolicyInterceptor:
    """Evaluates Helix policy at the tool seam."""

    def __init__(
        self,
        principal: Principal | None = None,
        *,
        on_decision=None,
    ) -> None:
        self.principal = principal
        self.on_decision = on_decision
        self.decisions: list[GuardResult] = []

    async def intercept(self, context: AgentContext) -> Verdict:
        if context.get("interception_point") != PRE_TOOL_CALL:
            return ALLOW

        principal = self.principal or current_principal()
        if principal is None:
            # Fail closed. In enforce mode the runtime blocks the guarded action
            # when an interceptor denies, which is the behaviour we want for a
            # call nobody can attribute.
            return Verdict.deny(
                reason="unattributable_call",
                message="No principal in scope; Helix governance refuses calls it cannot attribute.",
            )

        tool = _tool_name(context)
        if not tool:
            return Verdict.deny(
                reason="unidentifiable_tool",
                message="Helix governance could not determine which tool was being called.",
            )

        result = await guard_tool(principal, tool, _arguments(context), engine="hooks")
        self.decisions.append(result)
        if self.on_decision is not None:
            self.on_decision(result)

        if result.allowed:
            return ALLOW
        return Verdict.deny(reason=result.rule_id, message=result.message)


# At `pre_tool_call` the context carries `tool_call` = {"id", "name", "args"},
# while `target` is the tool's *arguments* - because arguments are what a
# transform at this point is allowed to rewrite. Read the name from `tool_call`
# and fall back to `target` only for the argument mapping.
def _tool_name(context: AgentContext) -> str | None:
    call = context.get("tool_call")
    if isinstance(call, dict):
        for key in ("name", "tool_name", "function_name"):
            value = call.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _arguments(context: AgentContext) -> dict[str, Any]:
    call = context.get("tool_call")
    if isinstance(call, dict):
        for key in ("args", "arguments", "parameters"):
            value = call.get(key)
            if isinstance(value, dict):
                return dict(value)
    target = context.get("target")
    return dict(target) if isinstance(target, dict) else {}


def build_hooks_middleware(
    principal: Principal | None = None,
    *,
    on_decision=None,
    mode: str = "enforce",
    record_sink=None,
):
    """The Agent Hooks bundle, ready to put first in an agent's middleware list.

    `mode="evaluate_only"` records what would have happened without blocking
    anything - the honest way to roll a policy out before enforcing it. The
    console shows which mode is active so nobody mistakes one for the other.
    """
    from agent_framework import create_agent_hooks_middleware

    from governance.events import bus

    interceptor = GovernancePolicyInterceptor(principal, on_decision=on_decision)

    def sink(record) -> None:
        # Interception records are payload-free by design, so this is safe to
        # put on a screen. The audit store is written by `enforce`, not here.
        bus().publish("hooks.record", summary=str(record)[:400])
        if record_sink is not None:
            record_sink(record)

    bundle = create_agent_hooks_middleware(
        {"helix-policy": interceptor},
        mode=mode,
        record_sink=sink,
    )
    return bundle, interceptor
