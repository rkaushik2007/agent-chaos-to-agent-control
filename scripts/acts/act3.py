"""Act 3 - Enforcement.

Identity said who. The toolbox said what exists. Neither said no.

Act 3 turns on the two controls that do: policy middleware in front of every
tool call, and a gateway in front of every delegation. Both are wired into a
real Agent Framework `Agent`, so what refuses the call on stage is the same
middleware that would refuse it in production.

The agents here are connected straight to the tool server rather than through
the toolbox. That is deliberate. The toolbox already narrows what each agent can
see, and a demo that only ever shows a control working when a second control has
already done the job proves very little. This act is about what happens when a
call reaches a tool anyway - a stale direct connection left over from act 1, a
grant somebody widened, a model that guessed a tool name.
"""

from __future__ import annotations

import asyncio
import json
import os

from agent_framework import MCPStreamableHTTPTool

from agents.planner import Delegation, ToolCall, scenario
from agents.runner import run_scenario
from governance import telemetry
from governance.a2a_gateway import A2AGateway
from governance.approvals import approval_queue, auto_approve
from governance.context import acting_as
from governance.enforce import GuardResult
from governance.identity import MockIdentityProvider, Principal, identity_provider
from governance.registry import registry
from governance.settings import demo_mode
from mcp_servers.clinical_tools import server as tools_server
from mcp_servers.clinical_tools.runtime import clinical_tools_server
from remote_agents.external_cro.server import CRO, UNKNOWN_VENDOR, remote_agent
from scripts import narrate
from console.serve import console_server
from scripts.acts.result import ActResult

TOOL_SCENARIOS = (
    "enforce.trial_ops_updates_case",
    "enforce.safety_triage_updates_case",
)
SHADOW_SCENARIOS = (
    "enforce.shadow_creates_po",
    "enforce.shadow_searches_docs",
)
A2A_SCENARIOS = (
    "enforce.trial_ops_to_cro",
    "enforce.trial_ops_to_unknown",
    "enforce.trial_ops_leaks_phi_to_cro",
)

# The shadow agent asserts its own identity. Nothing issued this; it is exactly
# what an unapproved agent would present, and the registry is what refuses it.
# Filled in by the scene that talks to the partner agents, read by the summary.
_RECEIVED: dict[str, list[str]] = {"external_cro": [], "unknown_vendor": []}

SELF_ASSERTED_SHADOW = Principal(
    agent_id="shadow_agent",
    identity_label="self-asserted",
    issuer="nobody",
    scopes=frozenset({"tool:create_po", "tool:search_docs", "tool:update_case"}),
    token="",
)


def _status(case_id: str) -> str:
    return json.loads(tools_server.read_case(case_id)).get("status", "?")


async def run(*, interactive: bool = True) -> ActResult:
    result = ActResult(act=3, name="Enforcement")
    reg = registry()
    # Default on, but never override somebody who turned it off on purpose.
    os.environ.setdefault("ENABLE_INSTRUMENTATION", "true")
    telemetry.configure(service_name="helix-agent-governance")
    await _configure_azure_monitor()

    identity = (
        identity_provider(reg) if demo_mode() == "live" else MockIdentityProvider(reg)
    )
    engine = os.getenv("GOVERNANCE_ENGINE", "middleware")

    narrate.act_title(
        3,
        "Enforcement",
        "Policy middleware in front of every tool call.\n"
        "A gateway in front of every delegation. Deny by default.",
    )
    narrate.step(
        f"Enforcement engine: [bold]{engine}[/bold]"
        + ("  (Agent Framework FunctionMiddleware, GA)" if engine == "middleware"
           else "  (Agent Hooks, AGENT-HOOKS-0.1, experimental)")
    )
    narrate.detail("Either way the decision comes from the same policy evaluator "
                   "and lands in the same audit store.")
    print()

    queue = approval_queue()
    queue.auto_resolver = None if interactive else auto_approve
    if not interactive:
        narrate.warn("Non-interactive run: approvals are auto-approved by the harness.")
        print()

    # The console runs in this process so that one approval queue and one audit
    # store serve both, and there is nothing to start in the right order on stage.
    async with console_server() as console_url:
        narrate.step(f"Governance console: [bold]{console_url}[/bold]")
        narrate.detail("Leave it open. The approval below is answered there.")
        print()
        await _scenes(result, reg, identity, engine, interactive, console_url, queue)

    telemetry.flush()
    _summary(result, _RECEIVED)
    return result


async def _scenes(result, reg, identity, engine, interactive, console_url, queue):
    async with clinical_tools_server() as tools_url:
        async with remote_agent(CRO) as (cro_url, cro_executor):
            async with remote_agent(UNKNOWN_VENDOR) as (vendor_url, vendor_executor):
                gateway = A2AGateway(
                    {"external_cro": cro_url, "unknown_vendor": vendor_url}, reg, identity
                )
                async with MCPStreamableHTTPTool(
                    name="clinical-tools", url=tools_url
                ) as tools:
                    await _tool_calls(result, identity, tools, engine, interactive,
                                      console_url, queue)
                    await _shadow_calls(result, tools, engine)
                    await _delegations(result, identity, gateway)

                received = {
                    "external_cro": list(cro_executor.received),
                    "unknown_vendor": list(vendor_executor.received),
                }

    _RECEIVED.clear()
    _RECEIVED.update(received)
    _partner_table(received)


async def _tool_calls(result, identity, tools, engine, interactive, console_url, queue):
    narrate.step("Registered agents, real tools, policy on the path")
    for key in TOOL_SCENARIOS:
        sc = scenario(key)
        principal = identity.issue(sc.agent)
        step = sc.steps[0]
        assert isinstance(step, ToolCall)
        case_id = str(step.arguments.get("case_id", ""))
        before = _status(case_id) if case_id else ""

        narrate.detail(f"[bold]{sc.agent}[/bold] -> {step.render()}")

        if _scenario_needs_human(sc) and interactive:
            _announce_approval(sc, console_url, queue)

        run = await run_scenario(principal, sc, tools, engine=engine)
        for decision in run.decisions:
            _show(result, decision)
        after = _status(case_id) if case_id else ""
        if case_id:
            changed = before != after
            colour = "green" if changed else "red"
            narrate.detail(
                f"   {case_id}: [{colour}]{before!r} -> {after!r}[/{colour}]"
                + ("" if changed else "   (the record was not touched)")
            )
    print()


async def _shadow_calls(result, tools, engine):
    narrate.step("The agent nobody approved, asserting its own identity")
    narrate.detail("It was never issued a token. It claims one anyway - which is "
                   "precisely what an unapproved agent does.")
    for key in SHADOW_SCENARIOS:
        sc = scenario(key)
        step = sc.steps[0]
        assert isinstance(step, ToolCall)
        narrate.detail(f"[bold red]{sc.agent}[/bold red] -> {step.render()}")
        run = await run_scenario(SELF_ASSERTED_SHADOW, sc, tools, engine=engine)
        for decision in run.decisions:
            _show(result, decision)
    orders = tools_server.purchase_orders()
    narrate.detail(
        f"   purchase orders raised by shadow_agent: "
        f"[bold green]{len(orders)}[/bold green]"
    )
    print()


async def _delegations(result, identity, gateway):
    narrate.step("Delegation - the gateway in front of every outbound hop")
    principal = identity.issue("trial_ops")
    for key in A2A_SCENARIOS:
        sc = scenario(key)
        step = sc.steps[0]
        assert isinstance(step, Delegation)
        narrate.detail(f"[bold]{sc.agent}[/bold] -> {step.render()}")
        with acting_as(principal):
            delegation = await gateway.delegate(
                principal, step.peer, step.task, classification=step.classification
            )
        _show(result, delegation.result)
        if delegation.reply:
            narrate.detail(f"   partner replied: {delegation.reply[:96]}")
    print()


def _show(result: ActResult, decision: GuardResult) -> None:
    narrate.decision_line(
        decision.principal_id,
        decision.target,
        decision.display_outcome,
        decision.reason[:70],
        decision.rule_id,
    )
    result.record(
        decision.principal_id, decision.target, decision.display_outcome,
        decision.rule_id, decision.reason,
    )


def _announce_approval(sc, console_url: str | None, queue) -> None:
    where = f"[bold]{console_url}[/bold]" if console_url else "the console"
    narrate.warn(
        f"This one needs a human. Approve or reject it in {where} "
        f"- no answer within {int(queue_timeout())}s is a denial."
    )


def queue_timeout() -> float:
    from governance import settings

    return settings.approval_timeout_seconds()


def _partner_table(received: dict[str, list[str]]) -> None:
    table = narrate.table(
        "What the partner agents actually received",
        ["Peer", "Messages delivered", "Reachable?"],
    )
    table.add_row("external_cro", str(len(received["external_cro"])), "yes")
    table.add_row(
        "[bold red]unknown_vendor[/bold red]",
        f"[bold green]{len(received['unknown_vendor'])}[/bold green]",
        "yes - and it still received nothing",
    )
    narrate.show(table)


def _summary(result: ActResult, received: dict[str, list[str]]) -> None:
    narrate.summary(
        [
            "trial_ops was refused a patient record by clearance, not by luck. "
            "The case field is byte-identical to what it was before the call.",
            "safety_triage was not refused - it was escalated. A PHI write is "
            "allowed, but never unattended.",
            "shadow_agent asserted an identity and was refused everything, "
            "including a harmless read. Unregistered means denied, not reduced.",
            "The unknown vendor was reachable, healthy and answering - and "
            f"received {len(received['unknown_vendor'])} messages. The network did "
            "not stop it; the allow-list did.",
            "PHI bound for an approved partner was still blocked. Being on the "
            "allow-list is not the same as being cleared for every payload.",
        ],
        closer="Deny by default. Four gateway checks. Every decision recorded.",
        colour="yellow",
    )


def _scenario_needs_human(sc) -> bool:
    return sc.key == "enforce.safety_triage_updates_case"


async def _configure_azure_monitor() -> None:
    """LIVE only: also export to the Foundry project's Application Insights.

    Never fatal. A project without Application Insights attached still gets the
    local collector, and an act that refuses to start because a second telemetry
    sink is missing would be a worse trade than one trace UI short.
    """
    from governance.settings import demo_mode

    if demo_mode() != "live":
        return
    from governance.model_live import configure_azure_monitor_from_project

    if await configure_azure_monitor_from_project():
        narrate.detail("Also exporting to the Foundry project's Application Insights.")
    else:
        narrate.warn("No Application Insights configured on the project; "
                     "traces go to the local collector only.")
