"""`uv run demo rehearse` - the gate before you walk on stage.

Runs all four acts in MOCK, non-interactively, auto-approving, and asserts every
decision the talk depends on. If an act still runs but now denies something it
used to allow, this fails and says which line changed.

The expectations below are written out in full rather than derived. Deriving
them from the code being tested would make this pass no matter what the code
did, which is the one thing a pre-flight check must never do.
"""

from __future__ import annotations

import os
import time
import traceback

from governance import telemetry
from governance.approvals import approval_queue, auto_approve
from governance.audit import audit_store
from scripts import narrate
from scripts.acts.result import ActResult

# (agent, target, outcome), in the order each act produces them.
EXPECTED: dict[int, list[tuple[str, str, str]]] = {
    1: [
        # Governance is off. Both of these succeed, and that is the point.
        ("trial_ops", "update_case", "ungoverned"),
        ("shadow_agent", "create_po", "ungoverned"),
    ],
    2: [
        ("trial_ops", "identity", "issued"),
        ("safety_triage", "identity", "issued"),
        ("supply", "identity", "issued"),
        ("shadow_agent", "identity", "deny"),
        ("trial_ops", "tools/list", "filtered"),
        ("safety_triage", "tools/list", "filtered"),
        ("supply", "tools/list", "filtered"),
        ("shadow_agent", "tools/list", "deny"),
        ("toolbox", "promote", "v2"),
        ("supply", "lookup_supplier", "available"),
        ("trial_ops", "tools/list-after-promote", "filtered"),
    ],
    3: [
        ("trial_ops", "update_case", "deny"),
        ("safety_triage", "update_case", "approved"),
        ("shadow_agent", "create_po", "deny"),
        ("shadow_agent", "search_docs", "deny"),
        ("trial_ops", "external_cro", "allow"),
        ("trial_ops", "unknown_vendor", "deny"),
        ("trial_ops", "external_cro", "deny"),
    ],
    4: [
        ("trial_ops", "search_docs", "allow"),
        ("safety_triage", "update_case", "timeout"),
        ("trial_ops", "safety_triage", "allow"),
    ],
}

# Beyond the decisions: the rules that produced them. A denial for the wrong
# reason is a different demo.
EXPECTED_RULES: dict[int, dict[tuple[str, str, str], str]] = {
    3: {
        ("trial_ops", "update_case", "deny"): "CLEARANCE-000",
        ("safety_triage", "update_case", "approved"): "R-201",
        ("shadow_agent", "create_po", "deny"): "REGISTRY-000",
        ("shadow_agent", "search_docs", "deny"): "REGISTRY-000",
        ("trial_ops", "unknown_vendor", "deny"): "A2A-CARD-000",
    },
    4: {
        ("trial_ops", "search_docs", "allow"): "R-100",
        ("safety_triage", "update_case", "timeout"): "R-201",
        ("trial_ops", "safety_triage", "allow"): "A2A-000",
    },
}


def _prepare_environment() -> None:
    os.environ["DEMO_MODE"] = "mock"
    os.environ.setdefault("GOVERNANCE_ENGINE", "middleware")
    # Act 4 lets an approval expire on purpose. Three seconds is enough to prove
    # it and short enough to keep the whole rehearsal brisk.
    os.environ.setdefault("ACT4_APPROVAL_TIMEOUT", "3")


async def rehearse() -> int:
    _prepare_environment()
    narrate.console.print()
    narrate.console.print(
        "[bold white]Rehearsal[/bold white]  four acts, MOCK, non-interactive, "
        "every decision asserted.\n"
    )

    failures: list[str] = []
    results: dict[int, ActResult] = {}
    started = time.monotonic()

    for act_number in (1, 2, 3, 4):
        # Acts 1 and 4 manage their own approval behaviour; acts 2 and 3 are
        # auto-approved so the rehearsal never waits for a human.
        approval_queue().auto_resolver = auto_approve if act_number in (2, 3) else None

        module = __import__(f"scripts.acts.act{act_number}", fromlist=["run"])
        began = time.monotonic()
        try:
            result = await module.run(interactive=False)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(f"act {act_number} raised {type(exc).__name__}: {exc}")
            narrate.error(f"act {act_number} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()
            continue
        results[act_number] = result
        failures.extend(_check(act_number, result))
        result.notes.append(f"{time.monotonic() - began:.1f}s")

    telemetry.flush()
    _report(results, failures, time.monotonic() - started)
    return 1 if failures else 0


def _check(act_number: int, result: ActResult) -> list[str]:
    problems: list[str] = []
    expected = EXPECTED[act_number]
    actual = [(o.agent, o.target, o.outcome) for o in result.observations]

    if actual != expected:
        problems.append(f"act {act_number}: decisions differ from the script")
        for line in _diff(expected, actual):
            problems.append(f"    {line}")

    for key, rule_id in EXPECTED_RULES.get(act_number, {}).items():
        match = next(
            (o for o in result.observations if (o.agent, o.target, o.outcome) == key),
            None,
        )
        if match is None:
            continue  # already reported by the decision diff above
        if match.rule_id != rule_id:
            problems.append(
                f"act {act_number}: {key[0]} {key[1]} was {key[2]} by "
                f"{match.rule_id}, expected {rule_id}"
            )

    if act_number == 4 and not result.trace_id:
        problems.append(
            "act 4: no trace id. Spans are not recording, so the trace links go "
            "nowhere. Check ENABLE_INSTRUMENTATION."
        )
    return problems


def _diff(expected: list[tuple], actual: list[tuple]) -> list[str]:
    lines = []
    for index in range(max(len(expected), len(actual))):
        want = expected[index] if index < len(expected) else None
        got = actual[index] if index < len(actual) else None
        if want != got:
            lines.append(f"[{index}] expected {want}, got {got}")
    return lines


def _report(results: dict[int, ActResult], failures: list[str], elapsed: float) -> None:
    table = narrate.table("Rehearsal", ["Act", "Name", "Decisions", "Result"])
    for act_number in (1, 2, 3, 4):
        result = results.get(act_number)
        act_failures = [f for f in failures if f.startswith(f"act {act_number}")]
        if result is None:
            status = "[bold red]DID NOT RUN[/bold red]"
        elif act_failures:
            status = "[bold red]MISMATCH[/bold red]"
        else:
            status = "[bold green]as scripted[/bold green]"
        table.add_row(
            str(act_number),
            result.name if result else "-",
            str(len(result.observations)) if result else "-",
            status,
        )
    narrate.show(table)

    if failures:
        narrate.console.print()
        for failure in failures:
            narrate.console.print(f"  [bold red]{failure}[/bold red]"
                                  if not failure.startswith("    ")
                                  else f"  [red]{failure}[/red]")
        narrate.console.print()
        narrate.error(
            f"Rehearsal FAILED in {elapsed:.0f}s. Do not go on stage until this is green."
        )
        return

    narrate.console.print(
        f"\n[bold green]Rehearsal passed[/bold green] in {elapsed:.0f}s. "
        f"{audit_store().count()} decisions recorded across four acts.\n"
        "Every decision the talk depends on came out as scripted.\n"
    )
