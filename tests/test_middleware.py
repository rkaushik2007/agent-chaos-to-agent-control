"""Milestone 4: the enforcement seam and both engines.

The two claims under test:

* A denial does not merely report a denial - the tool is never invoked, and the
  data is unchanged afterwards.
* `GOVERNANCE_ENGINE` changes which seam carries the decision, never the
  decision. Both engines are run over the same scenarios and their results are
  compared to each other, not just to expectations.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agent_framework import Agent, MCPStreamableHTTPTool, MiddlewareFailure

from agents.planner import ToolCall, scenario
from agents.runner import run_scenario
from agents.scripted_client import ScriptedChatClient
from governance.approvals import approval_queue, auto_approve, auto_reject
from governance.audit import audit_store
from governance.context import acting_as
from governance.enforce import guard_tool
from governance.identity import MockIdentityProvider, Principal
from governance.middleware import GovernanceFunctionMiddleware, arguments_as_dict
from governance.registry import registry
from mcp_servers.clinical_tools import server as clinical_tools
from mcp_servers.clinical_tools.runtime import clinical_tools_server

ENGINES = ("middleware", "hooks")

SELF_ASSERTED_SHADOW = Principal(
    agent_id="shadow_agent",
    identity_label="self-asserted",
    issuer="nobody",
    scopes=frozenset({"tool:update_case", "tool:search_docs", "tool:create_po"}),
    token="",
)


@pytest.fixture
def identity():
    return MockIdentityProvider(registry())


def status(case_id: str) -> str:
    return json.loads(clinical_tools.read_case(case_id))["status"]


# ---------------------------------------------------------------------------
# Deny short-circuits the tool
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine", ENGINES)
async def test_deny_prevents_the_tool_from_running(engine, identity):
    sc = scenario("enforce.trial_ops_updates_case")
    case_id = sc.steps[0].arguments["case_id"]
    before = status(case_id)

    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            run = await run_scenario(identity.issue("trial_ops"), sc, tools, engine=engine)

    assert [d.display_outcome for d in run.decisions] == ["deny"]
    assert run.decisions[0].rule_id == "CLEARANCE-000"
    # The point: not "it said no", but "nothing happened".
    assert status(case_id) == before


@pytest.mark.parametrize("engine", ENGINES)
async def test_an_unregistered_agent_is_denied_even_a_read(engine):
    sc = scenario("enforce.shadow_searches_docs")
    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            run = await run_scenario(SELF_ASSERTED_SHADOW, sc, tools, engine=engine)

    assert [d.display_outcome for d in run.decisions] == ["deny"]
    assert run.decisions[0].rule_id == "REGISTRY-000"


async def test_a_denial_reaches_the_model_as_a_readable_refusal(identity):
    """The agent must be able to say why, not just fail."""
    sc = scenario("enforce.trial_ops_updates_case")
    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            run = await run_scenario(identity.issue("trial_ops"), sc, tools)
    message = run.decisions[0].message
    assert "Denied by Helix governance" in message
    assert "CLEARANCE-000" in message
    assert "trial_ops" in message


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine", ENGINES)
async def test_approve_then_granted_lets_the_tool_run(engine, identity):
    approval_queue().auto_resolver = auto_approve
    sc = scenario("enforce.safety_triage_updates_case")
    case_id = sc.steps[0].arguments["case_id"]
    before = status(case_id)

    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            run = await run_scenario(identity.issue("safety_triage"), sc, tools, engine=engine)

    assert [d.display_outcome for d in run.decisions] == ["approved"]
    assert run.decisions[0].rule_id == "R-201"
    assert status(case_id) != before


@pytest.mark.parametrize("engine", ENGINES)
async def test_approve_then_rejected_stops_the_tool(engine, identity):
    approval_queue().auto_resolver = auto_reject
    sc = scenario("enforce.safety_triage_updates_case")
    case_id = sc.steps[0].arguments["case_id"]
    before = status(case_id)

    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            run = await run_scenario(identity.issue("safety_triage"), sc, tools, engine=engine)

    assert [d.display_outcome for d in run.decisions] == ["rejected"]
    assert status(case_id) == before


async def test_an_unanswered_approval_times_out_to_a_denial(identity, fast_approvals):
    """Silence is not consent, especially for a write to patient data."""
    approval_queue().auto_resolver = None
    sc = scenario("enforce.safety_triage_updates_case")
    case_id = sc.steps[0].arguments["case_id"]
    before = status(case_id)

    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            run = await run_scenario(identity.issue("safety_triage"), sc, tools)

    decision = run.decisions[0]
    assert decision.display_outcome == "timeout"
    assert decision.allowed is False
    assert "unanswered" in decision.reason
    assert status(case_id) == before


async def test_an_approval_can_be_resolved_from_another_thread(identity, fast_approvals):
    """The console runs on its own loop in its own thread; the button must work."""
    approval_queue().auto_resolver = None
    queue = approval_queue()

    async def answer_from_elsewhere():
        for _ in range(200):
            pending = queue.pending()
            if pending:
                # `resolve` is deliberately sync and thread-safe; call it the way
                # the console's request handler does.
                await asyncio.to_thread(
                    queue.resolve, pending[0].id, approved=True, resolved_by="dr-ops"
                )
                return True
            await asyncio.sleep(0.005)
        return False

    sc = scenario("enforce.safety_triage_updates_case")
    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            answered, run = await asyncio.gather(
                answer_from_elsewhere(),
                run_scenario(identity.issue("safety_triage"), sc, tools),
            )

    assert answered
    assert run.decisions[0].display_outcome == "approved"
    assert run.decisions[0].resolution.resolved_by == "dr-ops"


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

async def test_a_tool_call_with_no_principal_aborts_the_run():
    """A call the layer cannot attribute is not a call it may quietly allow."""
    sc = scenario("enforce.safety_triage_updates_case")
    steps = [s for s in sc.steps if isinstance(s, ToolCall)]

    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            async with Agent(
                client=ScriptedChatClient(steps),
                name="anonymous",
                instructions="x",
                tools=tools,
                middleware=[GovernanceFunctionMiddleware()],
            ) as agent:
                with pytest.raises(MiddlewareFailure, match="No principal in scope"):
                    await agent.run(sc.prompt)


async def test_a_broken_guard_aborts_the_run_rather_than_becoming_a_tool_error(
    monkeypatch, identity
):
    """Agent Framework turns an ordinary middleware exception into a tool error
    and carries on. For a bug inside the guard that is fail-open in spirit."""
    import governance.middleware as middleware_module

    async def exploding_guard(*args, **kwargs):
        raise RuntimeError("the policy store fell over")

    monkeypatch.setattr(middleware_module, "guard_tool", exploding_guard)

    sc = scenario("enforce.safety_triage_updates_case")
    steps = [s for s in sc.steps if isinstance(s, ToolCall)]
    principal = identity.issue("safety_triage")

    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
            async with Agent(
                client=ScriptedChatClient(steps),
                name="safety_triage",
                instructions="x",
                tools=tools,
                middleware=[GovernanceFunctionMiddleware(principal)],
            ) as agent:
                with pytest.raises(MiddlewareFailure, match="could not evaluate"):
                    await agent.run(sc.prompt)


# ---------------------------------------------------------------------------
# The two engines agree
# ---------------------------------------------------------------------------

async def test_both_engines_produce_identical_decisions(identity):
    """The claim the whole session rests on, asserted rather than asserted-to."""
    approval_queue().auto_resolver = auto_approve
    keys = (
        "enforce.trial_ops_updates_case",
        "enforce.safety_triage_updates_case",
        "enforce.shadow_creates_po",
    )

    per_engine = {}
    for engine in ENGINES:
        clinical_tools.reset_state()
        rows = []
        async with clinical_tools_server() as url:
            async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as tools:
                for key in keys:
                    sc = scenario(key)
                    principal = (
                        SELF_ASSERTED_SHADOW if sc.agent == "shadow_agent"
                        else identity.issue(sc.agent)
                    )
                    run = await run_scenario(principal, sc, tools, engine=engine)
                    rows.extend(
                        (d.principal_id, d.target, d.display_outcome, d.rule_id)
                        for d in run.decisions
                    )
        per_engine[engine] = rows

    assert per_engine["middleware"] == per_engine["hooks"]
    assert len(per_engine["middleware"]) == len(keys)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

async def test_every_decision_is_audited_with_its_reason(identity, isolated_audit_store):
    principal = identity.issue("trial_ops")
    with acting_as(principal):
        await guard_tool(principal, "update_case", {"case_id": "AE-0007"})
        await guard_tool(principal, "search_docs", {"query": "dosing"})

    rows = isolated_audit_store.recent()
    assert len(rows) == 2
    by_target = {row.target: row for row in rows}
    assert by_target["update_case"].decision == "deny"
    assert by_target["update_case"].rule_id == "CLEARANCE-000"
    assert by_target["search_docs"].decision == "allow"
    for row in rows:
        assert row.agent == "trial_ops"
        assert row.entra_agent_id == "mock:trial_ops"
        assert row.reason
        assert row.kind == "tool"
        assert row.arguments


async def test_an_approval_updates_its_own_audit_row(identity, isolated_audit_store):
    approval_queue().auto_resolver = auto_approve
    principal = identity.issue("safety_triage")
    with acting_as(principal):
        result = await guard_tool(principal, "update_case", {"case_id": "AE-0012"})

    assert result.display_outcome == "approved"
    row = isolated_audit_store.recent()[0]
    # One row, updated in place - not a second row that a reader has to correlate.
    assert isolated_audit_store.count() == 1
    assert row.decision == "approved"
    assert row.resolved_by == "rehearsal (auto)"


# ---------------------------------------------------------------------------
# Small things that would be annoying to get wrong
# ---------------------------------------------------------------------------

def test_arguments_are_normalised_from_whatever_the_framework_supplies():
    from pydantic import BaseModel

    class Args(BaseModel):
        case_id: str
        field: str

    assert arguments_as_dict({"a": 1}) == {"a": 1}
    assert arguments_as_dict(Args(case_id="AE-0001", field="status")) == {
        "case_id": "AE-0001", "field": "status",
    }
    assert arguments_as_dict(None) == {}
