"""Act 1 - Chaos.

Governance is off. There is no registry, no identity, no policy, no gateway and
no telemetry. Each agent holds its own credentials in a dotfile and opens its own
direct connection to the tool server. Everything they try, succeeds.

The only thing this act builds is the problem.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_framework import MCPStreamableHTTPTool

from agents.planner import ToolCall, scenario
from agents.toolresult import as_text
from governance.settings import REPO_ROOT
from mcp_servers.clinical_tools import server as tools_server
from mcp_servers.clinical_tools.runtime import clinical_tools_server
from scripts import narrate
from scripts.acts.result import ActResult

CHAOS_ENV = REPO_ROOT / ".env.chaos"

# Obviously fake. Generated at runtime and git-ignored, so the repo never carries
# anything that looks like a credential - which is the habit the act argues for.
CHAOS_ENV_BODY = """# Written by `uv run demo act1`. Git-ignored. Every value here is fake.
# This file exists to make a point: in act 1 each agent carries its own
# credentials, in its own file, with nobody tracking who holds what.
TRIAL_OPS_API_KEY=sk-FAKE-trialops-000000000000000000
SAFETY_TRIAGE_API_KEY=sk-FAKE-safety-0000000000000000000
SUPPLY_API_KEY=sk-FAKE-supply-0000000000000000000
SHADOW_AGENT_API_KEY=sk-FAKE-shadow-0000000000000000000
CLINICAL_TOOLS_URL=http://127.0.0.1:0/mcp
"""

SCENARIOS = ("chaos.trial_ops_updates_case", "chaos.shadow_creates_po")


def _ensure_chaos_env() -> list[str]:
    if not CHAOS_ENV.exists():
        CHAOS_ENV.write_text(CHAOS_ENV_BODY, encoding="utf-8")
    return [
        line.split("=", 1)[0]
        for line in CHAOS_ENV.read_text(encoding="utf-8").splitlines()
        if line.split("=", 1)[0].endswith("_API_KEY")
    ]


async def run(*, interactive: bool = True) -> ActResult:
    result = ActResult(act=1, name="Chaos")

    # Telemetry is genuinely off in act 1, not merely unexported.
    os.environ["ENABLE_INSTRUMENTATION"] = "false"

    narrate.act_title(
        1,
        "Chaos",
        "Four agents. No registry, no identity, no policy, no trace.\n"
        "Helix Therapeutics does not know this is happening.",
    )

    keys = _ensure_chaos_env()
    narrate.step(f"Each agent reads its own credentials from [bold]{CHAOS_ENV.name}[/bold]")
    for key in keys:
        narrate.detail(f"[red]{key}[/red] = sk-FAKE-... (fake, git-ignored, owned by nobody)")
    narrate.detail("Nobody can say which agent holds which key, or who issued it.")
    print()

    case_before = tools_server.read_case("AE-0007")
    status_before = _field(case_before, "status")

    async with clinical_tools_server() as url:
        narrate.step(f"clinical-tools MCP server listening on [bold]{url}[/bold]")
        narrate.detail("Every agent connects to it directly. There is no gateway in front of it.")
        print()

        for key in SCENARIOS:
            sc = scenario(key)
            narrate.step(f"[bold]{sc.agent}[/bold] was asked: [white]{sc.prompt}[/white]")

            # Each agent opens its own unfiltered session: no identity is
            # presented, and the full tool list is visible to all of them.
            async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as session:
                visible = sorted(f.name for f in session.functions)
                narrate.detail(f"identity presented: [red]none[/red]   "
                               f"tools visible: [red]{len(visible)}/{len(visible)} (all)[/red]")

                for step in sc.steps:
                    assert isinstance(step, ToolCall)
                    fn = next((f for f in session.functions if f.name == step.tool), None)
                    if fn is None:
                        raise RuntimeError(f"clinical-tools does not expose {step.tool}")
                    narrate.detail(f"calls [bold]{step.render()}[/bold]")
                    output = as_text(await fn.invoke(arguments=step.arguments), limit=120)
                    narrate.decision_line(sc.agent, step.tool, "ungoverned",
                                          "no check ran")
                    narrate.detail(f"   -> {output}")
                    result.record(sc.agent, step.tool, "ungoverned",
                                  reason="governance disabled")
            print()

        status_after = _field(tools_server.read_case("AE-0007"), "status")
        pos = tools_server.purchase_orders()

    damage = narrate.table("Damage done in the last 20 seconds",
                           ["What changed", "Who did it", "Authorised by"])
    damage.add_row(
        f"AE-0007 status: {status_before!r} -> {status_after!r}",
        "trial_ops (Clinical Operations)",
        "[bold red]nobody[/bold red]",
    )
    for po in pos:
        damage.add_row(
            f"{po['po_id']}: {po['qty']} x {po['item']}",
            "shadow_agent (unregistered)",
            "[bold red]nobody[/bold red]",
        )
    narrate.show(damage)

    gaps = narrate.table("What the governance layer recorded",
                         ["Control", "Records", "Status"])
    gaps.add_row("Agent registry", "0 agents", "[bold red]does not exist[/bold red]")
    gaps.add_row("Identity", "0 tokens issued", "[bold red]does not exist[/bold red]")
    gaps.add_row("Policy decisions", "0 evaluated", "[bold red]does not exist[/bold red]")
    gaps.add_row("Audit rows", "0 written", "[bold red]does not exist[/bold red]")
    gaps.add_row("Traces", "0 spans", "[bold red]disabled[/bold red]")
    narrate.show(gaps)

    narrate.summary(
        [
            "A Clinical Operations agent edited a patient safety record. "
            "It has no clinical reason to hold PHI and no clearance to write it.",
            "An agent nobody registered raised a 500-unit purchase order. "
            "It is not in any inventory, so no review would ever have caught it.",
            "Both calls succeeded because nothing was positioned to stop them. "
            "The tools do not defend themselves - tools never do.",
            "Worst of all: none of this is recoverable after the fact. "
            "There is no record that it happened.",
        ],
        closer="No identity. No policy. No trace.",
        colour="red",
    )

    return result


def _field(case_json: str, field: str) -> str:
    import json

    try:
        return str(json.loads(case_json)[field])
    except Exception:  # pragma: no cover - only reached if fixtures change
        return "?"


def cleanup() -> None:
    """Remove the chaos dotfile. Called by `uv run demo reset`."""
    Path(CHAOS_ENV).unlink(missing_ok=True)
