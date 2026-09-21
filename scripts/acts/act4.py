"""Act 4 - Visibility.

The controls from act 3 are still on. What act 4 adds is the ability to answer
the question an auditor actually asks, six weeks later: *what happened, who did
it, and why was it allowed or refused?*

One request, one trace id:

    trial_ops is asked about a case on its study
      -> search_docs                       ALLOW     it may read protocol text
      -> delegates to safety_triage        ALLOW     allow-listed, in depth
           -> update_case                  APPROVE   a PHI write needs a human
              ... and nobody answers       DENY      silence is not consent

Four decisions, two agents, one delegation hop, one trace. Act 1 ended on "no
trace". This is the same fleet with the same tools, and now every one of those
four lines is a span and a row.

This act deliberately lets the approval expire. An agent that delegates work at
3am and finds a human waiting is the happy path; the interesting question is
what the layer does when there is nobody there, and the answer has to be "deny".
"""

from __future__ import annotations

import json
import os
import webbrowser

from agents.planner import Delegation, ToolCall, scenario
from agents.runner import run_scenario
from console.serve import console_server
from governance import settings, telemetry
from governance.a2a_gateway import A2AGateway
from governance.approvals import approval_queue
from governance.audit import audit_store
from governance.context import acting_as
from governance.enforce import GuardResult
from governance.identity import MockIdentityProvider, identity_provider
from governance.registry import registry
from governance.toolbox import tool_source_factory
from mcp_servers.clinical_tools import server as tools_server
from mcp_servers.clinical_tools.runtime import clinical_tools_server
from scripts import narrate
from scripts.acts.result import ActResult

# Long enough for the audience to read the escalation on the console, short
# enough that the act stays under three minutes.
DEFAULT_TIMEOUT = "8"


async def run(*, interactive: bool = True) -> ActResult:
    result = ActResult(act=4, name="Visibility")
    reg = registry()

    # Default on, but never override somebody who turned it off on purpose.
    os.environ.setdefault("ENABLE_INSTRUMENTATION", "true")
    os.environ.setdefault("ENABLE_SENSITIVE_DATA", "true")
    os.environ["APPROVAL_TIMEOUT_SECONDS"] = os.getenv("ACT4_APPROVAL_TIMEOUT", DEFAULT_TIMEOUT)
    exporting = telemetry.configure(service_name=os.getenv("OTEL_SERVICE_NAME",
                                                           "helix-agent-governance"))
    await _configure_azure_monitor()

    identity = (
        identity_provider(reg) if settings.demo_mode() == "live"
        else MockIdentityProvider(reg)
    )
    queue = approval_queue()
    # Nobody answers, on purpose. In a non-interactive rehearsal the harness
    # would otherwise auto-approve and the act would prove the opposite point.
    queue.auto_resolver = None

    narrate.act_title(
        4,
        "Visibility",
        "One request. Two agents. A delegation hop. An escalation nobody answers.\n"
        "All of it under a single trace id.",
    )

    if not exporting:
        narrate.warn("Instrumentation is disabled, so spans go nowhere. "
                     "Set ENABLE_INSTRUMENTATION=true.")
    else:
        narrate.step(f"Exporting OTLP to [bold]{os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://localhost:4317')}[/bold]")
        narrate.detail("Agent Framework's own spans (invoke_agent, chat, execute_tool) "
                       "plus one governance.policy span per decision.")

    async with console_server() as console_url:
        narrate.step(f"Governance console: [bold]{console_url}[/bold]")
        narrate.step(f"Trace UI:           [bold]{settings.trace_ui_base_url()}[/bold]")
        if interactive:
            _open(console_url)
        print()

        trace_id = await _one_request(result, reg, identity, queue)

    # Push the spans out before telling anyone to go and look at them.
    telemetry.flush()

    _trace_table(result, trace_id)
    _summary(result, trace_id, console_url_hint=settings.trace_ui_base_url())
    return result


async def _one_request(result: ActResult, reg, identity, queue) -> str | None:
    outer = scenario("trace.delegated_triage")
    inner = scenario("trace.triage_write")
    trial_ops = identity.issue("trial_ops")

    async with clinical_tools_server() as upstream:
        # LocalToolbox in MOCK, the Foundry toolbox endpoint in LIVE.
        async with tool_source_factory()(
            upstream, identity, reg, version="v2"
        ) as toolbox:
            narrate.step(f"Tools reached through the toolbox at [bold]{toolbox.endpoint}[/bold] "
                         f"({toolbox.version})")

            async def triage(task: str) -> str:
                """What safety_triage does when trial_ops hands work to it."""
                safety = identity.issue("safety_triage")
                async with toolbox.session_for(safety) as tools:
                    run = await run_scenario(safety, inner, tools)
                for decision in run.decisions:
                    _show(result, decision, indent="      ")
                return run.text or "triage attempted"

            gateway = A2AGateway({}, reg, identity)
            gateway.register_internal("safety_triage", triage)

            with telemetry.root_span("helix.request delegated triage") as span:
                trace_id = telemetry.current_trace_id()
                narrate.detail(f"trace id [bold]{trace_id}[/bold]")
                print()
                narrate.step(f"[bold]trial_ops[/bold] was asked: "
                             f"[white]{outer.prompt}[/white]")

                tool_steps = [s for s in outer.steps if isinstance(s, ToolCall)]
                if tool_steps:
                    async with toolbox.session_for(trial_ops) as tools:
                        run = await run_scenario(trial_ops, outer, tools)
                    for decision in run.decisions:
                        _show(result, decision)

                for step in outer.steps:
                    if not isinstance(step, Delegation):
                        continue
                    narrate.detail(f"   hands off: [bold]{step.render()}[/bold]")
                    narrate.warn(
                        f"      the PHI write below is escalated. Nobody is going to "
                        f"answer it - watch it expire after "
                        f"{int(settings.approval_timeout_seconds())}s."
                    )
                    with acting_as(trial_ops):
                        # Reported as the gateway decides, so the hop appears
                        # above the work it let through rather than below it.
                        await gateway.delegate(
                            trial_ops, step.peer, step.task,
                            classification=step.classification,
                            on_decision=lambda d: _show(result, d, indent="   "),
                        )

                span.set_attribute("helix.scenario", outer.key)
            result.trace_id = trace_id
            print()

            case = json.loads(tools_server.read_case("AE-0012"))
            narrate.detail(
                f"AE-0012 status after all of that: [bold red]{case['status']!r}[/bold red] "
                "- unchanged, because nobody approved the write."
            )
            return trace_id


def _show(result: ActResult, decision: GuardResult, indent: str = "") -> None:
    narrate.console.print(
        f"{indent}   [bold white]{decision.principal_id:<14}[/bold white] "
        f"[white]{decision.target:<16}[/white] "
        f"[{narrate.DECISION_STYLE.get(decision.display_outcome, 'white')}]"
        f"{decision.display_outcome.upper():<9}"
        f"[/{narrate.DECISION_STYLE.get(decision.display_outcome, 'white')}] "
        f"[white]({decision.rule_id})[/white]  {narrate.clip(decision.reason, 96)}"
    )
    result.record(decision.principal_id, decision.target, decision.display_outcome,
                  decision.rule_id, decision.reason)


def _trace_table(result: ActResult, trace_id: str | None) -> None:
    if not trace_id:
        narrate.warn("No trace id - instrumentation was disabled for this run.")
        return
    rows = audit_store().by_trace(trace_id)
    table = narrate.table(
        f"Audit rows for trace {trace_id}",
        ["Agent", "Identity", "Target", "Decision", "Rule", "Class"],
    )
    for row in rows:
        style = narrate.DECISION_STYLE.get(row.decision, "white")
        table.add_row(
            row.agent,
            row.entra_agent_id or "-",
            row.target,
            f"[{style}]{row.decision.upper()}[/{style}]",
            row.rule_id,
            row.classification or "-",
        )
    narrate.show(table)
    result.notes.append(f"{len(rows)} audit rows under one trace")


def _summary(result: ActResult, trace_id: str | None, console_url_hint: str) -> None:
    narrate.summary(
        [
            f"Four decisions, two agents and one delegation hop share the trace id "
            f"{trace_id or '(none)'}. One question to an auditor, one answer.",
            "Every span carries entra.agent_id, governance.decision, "
            "governance.rule_id and data.classification - so 'show me every PHI "
            "write anyone attempted last quarter' is a query, not a project.",
            "The escalation expired and became a denial. The record shows an "
            "attempt, an escalation and a refusal - not silence.",
            "The console and the trace UI are reading the same decisions this "
            "terminal printed. There is one record, not three.",
            "This is the same fleet, the same tools and the same prompts as act 1. "
            "The difference is entirely the layer around them.",
        ],
        closer="From chaos to control: identity, a toolbox, a policy, a gateway, and a trace.",
        colour="green",
    )


def _open(console_url: str) -> None:
    """Open the console and the trace UI, best effort.

    Wrapped because a headless or locked-down machine must not take the act down
    with it - the URLs are printed above either way.
    """
    for url in (console_url, settings.trace_ui_base_url()):
        try:
            webbrowser.open_new_tab(url)
        except Exception:  # pragma: no cover - platform dependent
            narrate.warn(f"could not open a browser for {url}; open it yourself")


async def _configure_azure_monitor() -> None:
    """LIVE only: say whether spans are also reaching Application Insights.

    Configuration happens once, in `telemetry.configure()`, on the same provider
    as the local collector. This only reports the truth about it.
    """
    from governance.settings import demo_mode

    if demo_mode() != "live":
        return
    if telemetry.azure_monitor_active():
        narrate.detail("Also exporting to the Foundry project's Application Insights.")
    else:
        narrate.warn("No Application Insights configured on the project; "
                     "traces go to the local collector only.")
