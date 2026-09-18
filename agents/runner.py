"""Running a governed agent.

One function builds and runs an Agent Framework `Agent` with the governance
layer installed. The only thing that differs between MOCK and LIVE is the chat
client; the middleware list, the tools and the principal are constructed
identically.

The engine - `middleware` or `hooks` - chooses which seam carries the policy to
`governance.enforce`, never what the policy decides.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_framework import Agent

from agents.planner import Scenario, ToolCall
from agents.scripted_client import ScriptedChatClient
from governance import settings
from governance.context import acting_as
from governance.enforce import GuardResult
from governance.identity import Principal
from governance.middleware import GovernanceAgentMiddleware, GovernanceFunctionMiddleware


@dataclass
class AgentRun:
    """What one governed agent run produced."""

    agent_id: str
    prompt: str
    engine: str
    decisions: list[GuardResult] = field(default_factory=list)
    text: str = ""
    trace_id: str | None = None

    def outcome_for(self, target: str) -> str | None:
        for decision in self.decisions:
            if decision.target == target:
                return decision.display_outcome
        return None


def build_chat_client(steps: Sequence[ToolCall], *, final_text: str = "Done."):
    """MOCK: the scripted planner. LIVE: a Foundry model deployment."""
    if settings.demo_mode() == "live":
        from governance.model_live import foundry_chat_client

        return foundry_chat_client()
    return ScriptedChatClient(steps, final_text=final_text)


@contextlib.asynccontextmanager
async def governed_agent(
    principal: Principal,
    scenario: Scenario,
    tools: Any,
    *,
    engine: str | None = None,
    instructions: str | None = None,
) -> AsyncIterator[tuple[Agent, list[GuardResult]]]:
    """An `Agent` with the governance layer installed, and its decision log."""
    engine = engine or settings.governance_engine()
    decisions: list[GuardResult] = []
    tool_calls = [s for s in scenario.steps if isinstance(s, ToolCall)]
    client = build_chat_client(tool_calls)

    if engine == "hooks":
        from governance.hooks import build_hooks_middleware

        bundle, _interceptor = build_hooks_middleware(
            principal, on_decision=decisions.append
        )
        # The Agent Hooks bundle must be the outermost middleware so that it
        # forms the enforcement boundary; anything before it is outside that
        # boundary. The agent middleware after it is what puts the principal in
        # scope for anything that reads it from the context variable.
        middleware = [bundle, GovernanceAgentMiddleware(principal)]
    else:
        middleware = [
            GovernanceAgentMiddleware(principal),
            GovernanceFunctionMiddleware(principal, on_decision=decisions.append),
        ]

    async with Agent(
        client=client,
        id=principal.agent_id,
        name=principal.agent_id,
        instructions=instructions or f"You are the Helix {principal.agent_id} agent.",
        tools=tools,
        middleware=middleware,
    ) as agent:
        yield agent, decisions


async def run_scenario(
    principal: Principal,
    scenario: Scenario,
    tools: Any,
    *,
    engine: str | None = None,
) -> AgentRun:
    """Run one scripted scenario through the full governed agent path."""
    engine = engine or settings.governance_engine()
    from governance import telemetry

    async with governed_agent(principal, scenario, tools, engine=engine) as (agent, decisions):
        with acting_as(principal):
            response = await agent.run(scenario.prompt)

    return AgentRun(
        agent_id=principal.agent_id,
        prompt=scenario.prompt,
        engine=engine,
        decisions=decisions,
        text=(response.text or "").strip(),
        trace_id=telemetry.current_trace_id(),
    )
