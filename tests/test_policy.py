"""Milestone 2: the policy evaluator.

`governance.policy` is pure, so these are real assertions about the governance
posture rather than observations about a running demo. If the shipped
`config/policy.yaml` ever stops reproducing the matrix the talk puts on screen,
this file fails.
"""

from __future__ import annotations

import textwrap

import pytest

from governance.policy import ALLOW, APPROVE, DENY, Policy, PolicyInput
from governance.registry import load_registry, policy_input_for, registry


@pytest.fixture(scope="module")
def reg():
    return registry()


@pytest.fixture(scope="module")
def pol():
    return Policy.load()


def decide(pol, reg, agent: str, tool: str):
    return pol.evaluate(policy_input_for(reg, agent, tool))


# ---------------------------------------------------------------------------
# The matrix the session puts on screen. This is the contract.
# ---------------------------------------------------------------------------

MATRIX = {
    "trial_ops":     {"search_docs": ALLOW, "read_case": DENY,  "update_case": DENY,    "create_po": DENY},
    "safety_triage": {"search_docs": ALLOW, "read_case": ALLOW, "update_case": APPROVE, "create_po": DENY},
    "supply":        {"search_docs": ALLOW, "read_case": DENY,  "update_case": DENY,    "create_po": ALLOW},
    "shadow_agent":  {"search_docs": DENY,  "read_case": DENY,  "update_case": DENY,    "create_po": DENY},
}


@pytest.mark.parametrize(
    ("agent", "tool", "expected"),
    [(a, t, e) for a, row in MATRIX.items() for t, e in row.items()],
)
def test_the_matrix(pol, reg, agent, tool, expected):
    decision = decide(pol, reg, agent, tool)
    assert decision.decision == expected, f"{agent}/{tool}: {decision}"
    assert decision.rule_id, "every decision must name the rule that produced it"
    assert decision.reason, "every decision must carry a reason a human can read"


# ---------------------------------------------------------------------------
# Unregistered agents
# ---------------------------------------------------------------------------

def test_unregistered_agent_is_denied_before_anything_else(pol, reg):
    """Not registered means denied, and denied for that reason specifically."""
    for tool in list(reg.tools) + ["a_tool_that_does_not_exist"]:
        decision = decide(pol, reg, "shadow_agent", tool)
        assert decision.decision == DENY
        assert decision.rule_id == "REGISTRY-000"


def test_unregistered_agent_sees_no_tools(reg):
    assert reg.tools_for("shadow_agent") == ()
    assert reg.is_registered("shadow_agent") is False


def test_unregistered_agent_may_delegate_to_nobody(reg):
    for peer in reg.peers:
        assert reg.may_delegate_to("shadow_agent", peer) is False


# ---------------------------------------------------------------------------
# Data classification conditions
# ---------------------------------------------------------------------------

def test_clearance_is_the_reason_trial_ops_cannot_touch_phi(pol, reg):
    decision = decide(pol, reg, "trial_ops", "read_case")
    assert decision.rule_id == "CLEARANCE-000"
    assert "phi" in decision.reason


def test_clearance_is_the_reason_safety_triage_cannot_raise_a_po(pol, reg):
    decision = decide(pol, reg, "safety_triage", "create_po")
    assert decision.rule_id == "CLEARANCE-000"
    assert "financial" in decision.reason


def test_a_rules_classification_condition_is_honoured():
    """A rule scoped to one classification must not fire for another."""
    pol = Policy.load_from_text(
        """
        default_effect: deny
        rules:
          - id: R-PHI-ONLY
            agents: [analyst]
            tools: [reader]
            actions: [read]
            when:
              classification_in: [phi]
            effect: allow
        """
    )
    base = dict(agent="analyst", tool="reader", action="read", registered=True,
                clearance=frozenset({"phi", "financial"}), grants=frozenset({"reader"}))

    assert pol.evaluate(PolicyInput(classification="phi", **base)).decision == ALLOW
    financial = pol.evaluate(PolicyInput(classification="financial", **base))
    assert financial.decision == DENY
    assert financial.rule_id == "DEFAULT-DENY"


def test_structural_checks_cannot_be_overridden_by_a_permissive_rule():
    """The point of steps 1-4: a careless `allow` rule still cannot leak PHI."""
    pol = Policy.load_from_text(
        """
        default_effect: deny
        rules:
          - id: R-OOPS
            description: Somebody allowed everything to everyone.
            effect: allow
        """
    )
    # Registered, but without the clearance.
    decision = pol.evaluate(PolicyInput(
        agent="trial_ops", tool="read_case", action="read", classification="phi",
        registered=True, clearance=frozenset({"internal"}),
        grants=frozenset({"read_case"}),
    ))
    assert decision.decision == DENY
    assert decision.rule_id == "CLEARANCE-000"

    # Registered and cleared, but the tool was never granted.
    decision = pol.evaluate(PolicyInput(
        agent="trial_ops", tool="read_case", action="read", classification="phi",
        registered=True, clearance=frozenset({"internal", "phi"}),
        grants=frozenset({"search_docs"}),
    ))
    assert decision.decision == DENY
    assert decision.rule_id == "GRANT-000"


# ---------------------------------------------------------------------------
# Deny by default
# ---------------------------------------------------------------------------

def test_an_uncatalogued_tool_is_denied(pol, reg):
    decision = decide(pol, reg, "safety_triage", "exfiltrate_everything")
    assert decision.decision == DENY
    assert decision.rule_id == "CATALOGUE-000"


def test_no_matching_rule_means_deny():
    pol = Policy.load_from_text("default_effect: deny\nrules: []\n")
    decision = pol.evaluate(PolicyInput(
        agent="anyone", tool="anything", action="read", classification="internal",
        registered=True, clearance=frozenset({"internal"}), grants=frozenset({"anything"}),
    ))
    assert decision.decision == DENY
    assert decision.rule_id == "DEFAULT-DENY"


def test_every_shipped_rule_is_reachable(pol, reg):
    """A rule nobody can hit is a rule that is lying about the posture."""
    fired = {
        decide(pol, reg, agent, tool).rule_id
        for agent in reg.agents
        for tool in reg.tools
    }
    for rule in pol.rules:
        assert rule.id in fired, f"{rule.id} can never fire against the shipped registry"


# ---------------------------------------------------------------------------
# The policy file itself
# ---------------------------------------------------------------------------

def test_a_non_deny_default_is_rejected():
    with pytest.raises(ValueError, match="denies by default"):
        Policy.load_from_text("default_effect: allow\nrules: []\n")


def test_duplicate_rule_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate rule id"):
        Policy.load_from_text(
            """
            default_effect: deny
            rules:
              - {id: R-1, effect: allow}
              - {id: R-1, effect: deny}
            """
        )


def test_an_unknown_effect_is_rejected():
    with pytest.raises(ValueError, match="allow/deny/approve"):
        Policy.load_from_text("default_effect: deny\nrules: [{id: R-1, effect: maybe}]\n")


# ---------------------------------------------------------------------------
# The registry files themselves
# ---------------------------------------------------------------------------

def test_registry_rejects_a_toolbox_version_publishing_an_uncatalogued_tool(tmp_path):
    (tmp_path / "toolbox.yaml").write_text(textwrap.dedent("""
        server: {name: t}
        tools:
          known: {action: read, classification: internal}
        versions:
          v1: {tools: [known, mystery]}
        default_version: v1
    """), encoding="utf-8")
    (tmp_path / "agents.yaml").write_text("agents: {}\npeers: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="uncatalogued tools"):
        load_registry(tmp_path / "agents.yaml", tmp_path / "toolbox.yaml")


def test_every_registered_agent_has_an_owner_and_a_business_unit(reg):
    for record in reg.agents.values():
        assert "@" in record.owner, f"{record.id} has no accountable owner"
        assert record.business_unit != "unknown", f"{record.id} has no business unit"
        assert record.data_clearance, f"{record.id} has no declared clearance"


def test_every_granted_tool_is_catalogued(reg):
    for record in reg.agents.values():
        unknown = record.allowed_tools - set(reg.tools)
        assert not unknown, f"{record.id} is granted uncatalogued tools: {unknown}"


def test_every_allowed_peer_exists(reg):
    for record in reg.agents.values():
        for peer in record.allowed_peers:
            assert reg.peer(peer) is not None, f"{record.id} allow-lists unknown peer {peer}"
