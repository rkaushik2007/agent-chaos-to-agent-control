"""Milestone 3: identity and the toolbox endpoint.

The claim act 2 makes is that filtering happens at the endpoint, not in the
client. These tests prove it by going around the client entirely: a raw MCP
session that asks for a tool the endpoint never listed still gets refused.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from governance.identity import IdentityError, MockIdentityProvider, Principal
from governance.registry import registry
from governance.toolbox import local_toolbox
from mcp_servers.clinical_tools.runtime import clinical_tools_server


@pytest.fixture(scope="module")
def reg():
    return registry()


@pytest.fixture
def identity(reg):
    return MockIdentityProvider(reg, ttl_seconds=60)


@contextlib.asynccontextmanager
async def running_toolbox(identity, reg):
    async with clinical_tools_server() as upstream:
        async with local_toolbox(upstream, identity, reg) as toolbox:
            yield toolbox


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_registered_agents_get_an_identity(identity, reg):
    for agent_id in reg.agents:
        principal = identity.issue(agent_id)
        assert principal.agent_id == agent_id
        assert principal.token
        assert not principal.expired


def test_an_unregistered_agent_gets_no_identity_at_all(identity):
    """Not a weaker identity. None."""
    with pytest.raises(IdentityError, match="not in the agent registry"):
        identity.issue("shadow_agent")


def test_scopes_come_from_the_registry(identity, reg):
    principal = identity.issue("safety_triage")
    record = reg.require_agent("safety_triage")
    assert principal.tool_scopes == record.allowed_tools
    assert principal.peer_scopes == frozenset(record.allowed_peers)


def test_a_token_round_trips(identity):
    issued = identity.issue("supply")
    verified = identity.verify(issued.token)
    assert verified.agent_id == issued.agent_id
    assert verified.scopes == issued.scopes
    assert verified.identity_label == issued.identity_label


def test_a_tampered_token_does_not_verify(identity):
    token = identity.issue("trial_ops").token
    body, signature = token.rsplit(".", 1)
    forged = identity.issue("safety_triage").token.rsplit(".", 1)[0]
    with pytest.raises(IdentityError, match="signature"):
        identity.verify(f"{forged}.{signature}")
    with pytest.raises(IdentityError, match="signature"):
        identity.verify(f"{body}.{'A' * len(signature)}")
    with pytest.raises(IdentityError, match="malformed"):
        identity.verify("not-a-token")


def test_an_expired_token_does_not_verify(reg):
    provider = MockIdentityProvider(reg, ttl_seconds=-1)
    token = provider.issue("supply").token
    with pytest.raises(IdentityError, match="expired"):
        provider.verify(token)


def test_a_token_from_another_issuer_does_not_verify(reg):
    """A per-process key means a token from one rehearsal is useless in the next."""
    token = MockIdentityProvider(reg).issue("supply").token
    with pytest.raises(IdentityError, match="signature"):
        MockIdentityProvider(reg).verify(token)


# ---------------------------------------------------------------------------
# The toolbox endpoint
# ---------------------------------------------------------------------------

async def test_the_endpoint_filters_per_caller(identity, reg):
    async with running_toolbox(identity, reg) as toolbox:
        seen = {}
        for agent_id in ("trial_ops", "safety_triage", "supply"):
            async with toolbox.session_for(identity.issue(agent_id)) as session:
                seen[agent_id] = sorted(f.name for f in session.functions)

    assert seen["trial_ops"] == ["search_docs"]
    assert seen["safety_triage"] == ["read_case", "search_docs", "update_case"]
    assert seen["supply"] == ["create_po", "search_docs"]


async def test_an_unauthenticated_caller_is_shown_nothing(identity, reg):
    async with running_toolbox(identity, reg) as toolbox:
        async with streamablehttp_client(toolbox.endpoint) as (read, write, _):
            async with ClientSession(read, write,
                                     read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert listed.tools == []

                # ...and cannot call one it was never shown.
                result = await session.call_tool("search_docs", {"query": "dosing"})
                assert result.isError
                assert "requires an identity" in str(result.content)


async def test_the_endpoint_refuses_a_tool_it_did_not_publish(identity, reg):
    """The proof that the filter is a control, not a client-side preference.

    `supply` is never shown `update_case`. This bypasses the client entirely and
    asks the endpoint for it anyway.
    """
    async with running_toolbox(identity, reg) as toolbox:
        principal = identity.issue("supply")
        headers = identity.bearer_headers(principal)
        async with streamablehttp_client(toolbox.endpoint, headers=headers) as (read, write, _):
            async with ClientSession(read, write,
                                     read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                assert "update_case" not in {t.name for t in (await session.list_tools()).tools}

                result = await session.call_tool(
                    "update_case", {"case_id": "AE-0001", "field": "status", "value": "closed"}
                )
                assert result.isError
                assert "not available to supply" in str(result.content)

        assert ("supply", "update_case", "not published to this agent by the toolbox") \
            in toolbox.refusals


async def test_a_forged_token_buys_nothing(identity, reg):
    async with running_toolbox(identity, reg) as toolbox:
        forged = Principal(
            agent_id="shadow_agent",
            identity_label="mock:shadow_agent",
            issuer="attacker",
            scopes=frozenset({"tool:create_po", "tool:update_case"}),
            token="not.a.real.token",
            expires_at=2 ** 31,
        )
        async with toolbox.session_for(forged) as session:
            assert [f.name for f in session.functions] == []


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

async def test_promoting_a_version_needs_no_agent_restart(identity, reg):
    async with running_toolbox(identity, reg) as toolbox:
        assert toolbox.version == "v1"
        principal = identity.issue("supply")

        async with toolbox.session_for(principal) as session:
            before = sorted(f.name for f in session.functions)
        assert "lookup_supplier" not in before

        promotion = toolbox.promote("v2")
        assert promotion.previous == "v1"
        assert promotion.current == "v2"
        assert promotion.added == ("lookup_supplier",)
        assert promotion.removed == ()

        # Same endpoint, same identity, same process. Nothing was redeployed.
        async with toolbox.session_for(principal) as session:
            after = sorted(f.name for f in session.functions)
        assert sorted(set(after) - set(before)) == ["lookup_supplier"]


async def test_a_promotion_grants_nothing_to_an_agent_without_the_grant(identity, reg):
    async with running_toolbox(identity, reg) as toolbox:
        principal = identity.issue("trial_ops")
        toolbox.promote("v2")
        async with toolbox.session_for(principal) as session:
            assert sorted(f.name for f in session.functions) == ["search_docs"]


async def test_promoting_an_unknown_version_fails_loudly(identity, reg):
    async with running_toolbox(identity, reg) as toolbox:
        with pytest.raises(KeyError, match="Unknown toolbox version"):
            toolbox.promote("v99")
        assert toolbox.version == "v1", "a failed promotion must not change the default"
