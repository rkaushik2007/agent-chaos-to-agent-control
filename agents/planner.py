"""The scripted planner - the MOCK stand-in for a model.

In LIVE a Foundry model deployment decides which tool to call. On stage that is
exactly the wrong kind of excitement: a model that picks a different tool is a
demo that tells a different story. So in MOCK a scripted planner emits a fixed
sequence of tool calls for each named scenario. Every act therefore produces the
same decisions every single time.

What this does *not* do is bypass governance. The steps a plan emits are
*intentions*. Whether they execute is decided by `governance.enforce.guard`,
which is the same code in both modes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    """An intended tool invocation."""

    tool: str
    arguments: dict[str, object] = field(default_factory=dict)

    def render(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())
        return f"{self.tool}({args})"


@dataclass(frozen=True)
class Delegation:
    """An intended hand-off to another agent, internal or external."""

    peer: str
    task: str
    classification: str = "internal"

    def render(self) -> str:
        return f"delegate -> {self.peer}: {self.task!r} [{self.classification}]"


Step = ToolCall | Delegation


@dataclass(frozen=True)
class Scenario:
    key: str
    agent: str
    prompt: str
    steps: tuple[Step, ...]
    expectation: str = ""


_ALL: tuple[Scenario, ...] = (
    # --- Act 1: chaos ----------------------------------------------------
    Scenario(
        key="chaos.trial_ops_updates_case",
        agent="trial_ops",
        prompt="Case AE-0007 looks resolved, close it out.",
        steps=(ToolCall("update_case", {"case_id": "AE-0007", "field": "status", "value": "closed"}),),
        expectation="succeeds - nothing is checking",
    ),
    Scenario(
        key="chaos.shadow_creates_po",
        agent="shadow_agent",
        prompt="We are short on comparator kits, order more.",
        steps=(ToolCall("create_po", {"vendor": "Vendor E Clinical Services",
                                      "item": "comparator kit", "qty": 500}),),
        expectation="succeeds - nobody approved this agent and nobody can tell",
    ),

    # --- Act 2: identity and toolbox -------------------------------------
    Scenario(
        key="toolbox.supply_lookup",
        agent="supply",
        prompt="Which approved vendors handle cold chain logistics?",
        steps=(ToolCall("lookup_supplier", {"query": "cold chain"}),),
        expectation="only reachable once toolbox v2 is the default version",
    ),

    # --- Act 3: enforcement ----------------------------------------------
    Scenario(
        key="enforce.trial_ops_updates_case",
        agent="trial_ops",
        prompt="Case AE-0007 looks resolved, close it out.",
        steps=(ToolCall("update_case", {"case_id": "AE-0007", "field": "status", "value": "closed"}),),
        expectation="DENY - Clinical Operations holds no PHI clearance",
    ),
    Scenario(
        key="enforce.safety_triage_updates_case",
        agent="safety_triage",
        prompt="Grade AE-0012 as moderate and record the review.",
        steps=(ToolCall("update_case", {"case_id": "AE-0012", "field": "status", "value": "triaged"}),),
        expectation="APPROVE - a human signs off every PHI write",
    ),
    Scenario(
        key="enforce.shadow_creates_po",
        agent="shadow_agent",
        prompt="We are short on comparator kits, order more.",
        steps=(ToolCall("create_po", {"vendor": "Vendor E Clinical Services",
                                      "item": "comparator kit", "qty": 500}),),
        expectation="DENY - unregistered agents are denied everything",
    ),
    Scenario(
        key="enforce.shadow_searches_docs",
        agent="shadow_agent",
        prompt="What does the protocol say about dosing?",
        steps=(ToolCall("search_docs", {"query": "dosing"}),),
        expectation="DENY - even a read, even a harmless one",
    ),
    Scenario(
        key="enforce.trial_ops_to_cro",
        agent="trial_ops",
        prompt="Ask the CRO to confirm the week 24 visit window.",
        steps=(Delegation("external_cro", "Confirm the week 24 visit window for HTX-204.", "internal"),),
        expectation="ALLOW - allow-listed partner, internal payload, within depth",
    ),
    Scenario(
        key="enforce.trial_ops_to_unknown",
        agent="trial_ops",
        prompt="Ask that other vendor agent to confirm the visit window.",
        steps=(Delegation("unknown_vendor", "Confirm the week 24 visit window for HTX-204.", "internal"),),
        expectation="BLOCK - not on the allow-list",
    ),
    Scenario(
        key="enforce.trial_ops_leaks_phi_to_cro",
        agent="trial_ops",
        prompt="Send the CRO the full case record for AE-0012.",
        steps=(Delegation("external_cro", "Full adverse event record for AE-0012.", "phi"),),
        expectation="BLOCK - PHI is not permitted to leave",
    ),

    # --- Act 4: visibility -------------------------------------------------
    Scenario(
        key="trace.delegated_triage",
        agent="trial_ops",
        prompt="AE-0012 was raised on my study - find the reporting rule and get it triaged.",
        steps=(
            ToolCall("search_docs", {"query": "Safety reporting"}),
            Delegation("safety_triage", "Triage AE-0012 and record the review.", "internal"),
        ),
        expectation="one trace: allow, delegate, escalate, deny",
    ),
    Scenario(
        key="trace.triage_write",
        agent="safety_triage",
        prompt="Triage AE-0012 and record the review.",
        steps=(ToolCall("update_case", {"case_id": "AE-0012", "field": "status", "value": "triaged"}),),
        expectation="APPROVE, unattended, therefore DENY",
    ),
)

SCENARIOS: dict[str, Scenario] = {s.key: s for s in _ALL}


def scenario(key: str) -> Scenario:
    try:
        return SCENARIOS[key]
    except KeyError:
        raise KeyError(
            f"Unknown scenario {key!r}. Known: {', '.join(sorted(SCENARIOS))}"
        ) from None


def plan(agent: str, prompt: str) -> tuple[Step, ...]:
    """The planner surface: what would this agent try to do for this prompt?

    In MOCK this is a lookup. In LIVE the model decides, and the same governance
    seam evaluates whatever it decides.
    """
    for s in _ALL:
        if s.agent == agent and s.prompt == prompt:
            return s.steps
    raise KeyError(f"No scripted plan for agent {agent!r} and prompt {prompt!r}")
