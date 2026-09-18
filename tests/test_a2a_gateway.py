"""Milestone 4: the A2A gateway.

Each of the four checks is exercised on its own, because each catches something
the other three cannot. A suite that only tested `evaluate()` end to end would
pass with three of them deleted.
"""

from __future__ import annotations

import pytest

from governance.a2a_gateway import (
    CHECK_CARD,
    CHECK_DEPTH,
    CHECK_EGRESS,
    CHECK_GRANT,
    A2AGateway,
    AgentCardInfo,
    check_card,
    check_depth,
    check_egress,
    check_grant,
    evaluate,
)
from governance.context import acting_as
from governance.identity import MockIdentityProvider, Principal
from governance.policy import ALLOW, DENY
from governance.registry import registry
from remote_agents.external_cro.server import CRO, UNKNOWN_VENDOR, remote_agent

CRO_CARD = AgentCardInfo(
    name="Northwind CRO Study Agent",
    url="http://partner.example",
    organisation="Northwind Clinical Research",
)


@pytest.fixture(scope="module")
def reg():
    return registry()


@pytest.fixture
def identity(reg):
    return MockIdentityProvider(reg)


@pytest.fixture
def trial_ops(identity):
    return identity.issue("trial_ops")


def principal_with(agent_id: str, *peers: str) -> Principal:
    return Principal(
        agent_id=agent_id,
        identity_label=f"mock:{agent_id}",
        issuer="test",
        scopes=frozenset(f"peer:{p}" for p in peers),
        token="",
    )


# ---------------------------------------------------------------------------
# Check 1 - the agent card and the allow-list
# ---------------------------------------------------------------------------

def test_card_check_passes_for_an_allow_listed_peer(reg):
    assert check_card(reg, "external_cro", CRO_CARD) is None


def test_card_check_refuses_a_peer_nobody_registered(reg):
    decision = check_card(reg, "some_random_agent", CRO_CARD)
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_CARD
    assert "ever registered" in decision.reason


def test_card_check_refuses_a_peer_that_is_not_allow_listed(reg):
    card = AgentCardInfo(name="Vendor Assistant", url="http://vendor.example")
    decision = check_card(reg, "unknown_vendor", card)
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_CARD
    assert "allow-list" in decision.reason


def test_card_check_refuses_a_peer_that_serves_no_card(reg):
    decision = check_card(reg, "external_cro",
                          AgentCardInfo.unresolvable("http://partner.example"))
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_CARD
    assert "resolvable agent card" in decision.reason


def test_card_check_refuses_when_something_else_answers_at_the_address(reg):
    """The allow-list names an agent, not a URL.

    If a different agent is answering where the partner used to be, the
    allow-list entry does not cover it.
    """
    impostor = AgentCardInfo(name="Totally Legitimate Agent", url="http://partner.example")
    decision = check_card(reg, "external_cro", impostor)
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_CARD
    assert "identifies as" in decision.reason


def test_card_check_does_not_require_a_card_from_an_internal_peer(reg):
    assert check_card(reg, "safety_triage", None) is None


# ---------------------------------------------------------------------------
# Check 2 - may this caller delegate to this peer
# ---------------------------------------------------------------------------

def test_grant_check_passes_for_a_registered_pairing(reg, trial_ops):
    assert check_grant(reg, trial_ops, "external_cro") is None


def test_grant_check_refuses_a_peer_the_caller_is_not_registered_for(reg, identity):
    supply = identity.issue("supply")
    decision = check_grant(reg, supply, "external_cro")
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_GRANT
    assert "not registered to delegate" in decision.reason


def test_grant_check_refuses_an_identity_whose_token_lacks_the_scope(reg):
    """The registry and the presented identity must agree.

    A token minted before the grant existed does not silently acquire it.
    """
    stale = principal_with("trial_ops")  # registered for the peer, no scope in hand
    assert reg.may_delegate_to("trial_ops", "external_cro") is True
    decision = check_grant(reg, stale, "external_cro")
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_GRANT
    assert "peer:external_cro scope" in decision.reason


# ---------------------------------------------------------------------------
# Check 3 - delegation depth
# ---------------------------------------------------------------------------

def test_depth_check_passes_within_the_limit(reg, trial_ops):
    record = reg.require_agent("trial_ops")
    assert record.max_delegation_depth == 2
    assert check_depth(reg, trial_ops, 0) is None
    assert check_depth(reg, trial_ops, 1) is None


def test_depth_check_refuses_one_hop_too_far(reg, trial_ops):
    decision = check_depth(reg, trial_ops, 2)
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_DEPTH
    assert "exceeds the limit of 2" in decision.reason


def test_depth_check_is_per_agent(reg, identity):
    safety = identity.issue("safety_triage")
    assert reg.require_agent("safety_triage").max_delegation_depth == 1
    assert check_depth(reg, safety, 0) is None
    assert check_depth(reg, safety, 1).rule_id == CHECK_DEPTH


def test_an_unregistered_agent_has_no_delegation_budget(reg):
    decision = check_depth(reg, principal_with("shadow_agent", "external_cro"), 0)
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_DEPTH


# ---------------------------------------------------------------------------
# Check 4 - may this payload leave
# ---------------------------------------------------------------------------

def test_egress_check_passes_for_a_permitted_classification(reg):
    assert check_egress(reg, "external_cro", "internal") is None


def test_egress_check_refuses_phi_to_an_approved_partner(reg):
    """Allow-listed is not the same as cleared for everything."""
    decision = check_egress(reg, "external_cro", "phi")
    assert decision.decision == DENY
    assert decision.rule_id == CHECK_EGRESS
    assert "phi data may not leave" in decision.reason


def test_egress_check_refuses_financial_data_too(reg):
    assert check_egress(reg, "external_cro", "financial").rule_id == CHECK_EGRESS


# ---------------------------------------------------------------------------
# All four together
# ---------------------------------------------------------------------------

def test_evaluate_allows_only_when_all_four_pass(reg, trial_ops):
    decision = evaluate(reg, trial_ops, "external_cro",
                        classification="internal", current_depth=0, card=CRO_CARD)
    assert decision.decision == ALLOW
    assert decision.reason == "all four gateway checks passed"


@pytest.mark.parametrize(
    ("peer", "classification", "depth", "card", "expected_rule"),
    [
        ("unknown_vendor", "internal", 0, CRO_CARD, CHECK_CARD),
        ("external_cro", "internal", 0, AgentCardInfo.unresolvable("u"), CHECK_CARD),
        ("external_cro", "internal", 2, CRO_CARD, CHECK_DEPTH),
        ("external_cro", "phi", 0, CRO_CARD, CHECK_EGRESS),
    ],
)
def test_evaluate_reports_which_check_refused(
    reg, trial_ops, peer, classification, depth, card, expected_rule
):
    decision = evaluate(reg, trial_ops, peer, classification=classification,
                        current_depth=depth, card=card)
    assert decision.decision == DENY
    assert decision.rule_id == expected_rule


def test_grant_is_reported_when_the_card_is_fine_but_the_caller_is_not(reg, identity):
    supply = identity.issue("supply")
    decision = evaluate(reg, supply, "external_cro",
                        classification="internal", current_depth=0, card=CRO_CARD)
    assert decision.rule_id == CHECK_GRANT


# ---------------------------------------------------------------------------
# The gateway against real partner agents
# ---------------------------------------------------------------------------

async def test_an_allowed_hop_reaches_the_partner(reg, identity, trial_ops):
    async with remote_agent(CRO) as (url, executor):
        gateway = A2AGateway({"external_cro": url}, reg, identity)
        with acting_as(trial_ops):
            delegation = await gateway.delegate(
                trial_ops, "external_cro",
                "Confirm the week 24 visit window for HTX-204.",
            )

    assert delegation.allowed
    assert "day 168" in delegation.reply
    assert executor.received == ["Confirm the week 24 visit window for HTX-204."]


async def test_a_blocked_hop_never_reaches_the_partner(reg, identity, trial_ops):
    """The vendor is up, healthy and answering. It still hears nothing."""
    async with remote_agent(UNKNOWN_VENDOR) as (url, executor):
        gateway = A2AGateway({"unknown_vendor": url}, reg, identity)
        with acting_as(trial_ops):
            delegation = await gateway.delegate(
                trial_ops, "unknown_vendor", "Anything at all.",
            )

    assert not delegation.allowed
    assert delegation.result.rule_id == CHECK_CARD
    assert delegation.reply is None
    assert executor.received == []


async def test_phi_never_leaves_even_for_an_allow_listed_partner(reg, identity, trial_ops):
    async with remote_agent(CRO) as (url, executor):
        gateway = A2AGateway({"external_cro": url}, reg, identity)
        with acting_as(trial_ops):
            delegation = await gateway.delegate(
                trial_ops, "external_cro",
                "Full adverse event record for AE-0012.",
                classification="phi",
            )

    assert not delegation.allowed
    assert delegation.result.rule_id == CHECK_EGRESS
    assert executor.received == []


async def test_the_gateway_resolves_a_live_card_and_notices_an_impostor(reg, identity, trial_ops):
    """Point the allow-listed peer id at the wrong service and it is refused."""
    async with remote_agent(UNKNOWN_VENDOR) as (url, executor):
        gateway = A2AGateway({"external_cro": url}, reg, identity)
        card = await gateway.fetch_card("external_cro")
        assert card.name == "Vendor Assistant"

        with acting_as(trial_ops):
            delegation = await gateway.delegate(trial_ops, "external_cro", "Hello.")

    assert not delegation.allowed
    assert delegation.result.rule_id == CHECK_CARD
    assert executor.received == []


async def test_outbound_headers_carry_depth_chain_and_trace_context(reg, identity, trial_ops):
    gateway = A2AGateway({}, reg, identity)
    headers = gateway.outbound_headers(trial_ops, depth=1, chain=("orchestrator",))
    assert headers["x-helix-delegation-depth"] == "2"
    assert headers["x-helix-delegation-chain"] == "orchestrator -> trial_ops"
    assert headers["x-helix-calling-agent"] == "trial_ops"


async def test_an_internal_hop_runs_one_level_deeper(reg, identity, trial_ops):
    """Depth has to be carried across the hop, or every callee starts at zero."""
    seen: list[tuple[str, int]] = []

    async def triage(task: str) -> str:
        from governance.context import current_principal, delegation_depth

        principal = current_principal()
        seen.append((principal.agent_id if principal else "?", delegation_depth()))
        return "triaged"

    gateway = A2AGateway({}, reg, identity)
    gateway.register_internal("safety_triage", triage)

    with acting_as(trial_ops):
        delegation = await gateway.delegate(trial_ops, "safety_triage", "Triage AE-0012.")

    assert delegation.allowed
    assert delegation.reply == "triaged"
    assert seen == [("safety_triage", 1)]
