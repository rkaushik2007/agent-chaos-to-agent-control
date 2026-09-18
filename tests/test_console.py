"""Milestone 5: the governance console.

The console is the only part of the demo an audience reads rather than watches,
so these tests care about two things: that an unregistered agent is impossible
to miss, and that the Approve button actually resolves an act that is waiting.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from console.app import app, inventory_rows
from governance.approvals import approval_queue
from governance.context import acting_as
from governance.enforce import guard_tool
from governance.identity import MockIdentityProvider, Principal
from governance.registry import registry

SHADOW = Principal(
    agent_id="shadow_agent",
    identity_label="self-asserted",
    issuer="nobody",
    token="",
)


@pytest.fixture
def identity():
    return MockIdentityProvider(registry())


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
        yield c


async def seed_decisions(identity) -> None:
    trial_ops = identity.issue("trial_ops")
    with acting_as(trial_ops):
        await guard_tool(trial_ops, "search_docs", {"query": "dosing"})
        await guard_tool(trial_ops, "update_case", {"case_id": "AE-0007"})
    with acting_as(SHADOW):
        await guard_tool(SHADOW, "create_po", {"vendor": "V", "item": "kit", "qty": 1})


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

async def test_the_page_renders_all_four_panels(client, identity):
    await seed_decisions(identity)
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text
    for panel in ("Approval queue", "Live decisions", "Agent inventory"):
        assert panel in body
    assert "trial_ops" in body and "search_docs" in body


async def test_a_decision_with_a_trace_id_links_into_the_trace_ui(
    client, isolated_audit_store
):
    """The suite runs with instrumentation off, so a trace id is seeded here.

    What matters is that a row carrying one becomes a clickable link, not that
    the test process happens to have a collector.
    """
    from governance.audit import AuditEntry

    isolated_audit_store.record(
        AuditEntry(
            agent="trial_ops", kind="tool", target="search_docs", decision="allow",
            rule_id="R-100", reason="permitted", trace_id="a" * 32,
        )
    )
    body = (await client.get("/panels/decisions")).text
    assert f'href="/trace/{"a" * 32}"' in body
    assert "aaaaaaaa<" in body, "the link text is the short form of the trace id"


async def test_htmx_is_served_locally_not_from_a_cdn(client):
    """MOCK has to work with the network unplugged."""
    body = (await client.get("/")).text
    assert "/static/htmx.min.js" in body
    assert "unpkg.com" not in body and "cdn." not in body
    assert (await client.get("/static/htmx.min.js")).status_code == 200


async def test_an_unregistered_agent_is_flagged_in_the_inventory(client, identity):
    await seed_decisions(identity)
    rows = {row["id"]: row for row in inventory_rows()}
    assert rows["shadow_agent"]["registered"] is False
    assert rows["trial_ops"]["registered"] is True

    body = (await client.get("/panels/inventory")).text
    assert "NOT IN REGISTRY" in body
    assert "unregistered" in body  # the row class that paints it red


async def test_the_feed_shows_the_rule_that_refused(client, identity):
    await seed_decisions(identity)
    body = (await client.get("/panels/decisions")).text
    assert "CLEARANCE-000" in body
    assert "REGISTRY-000" in body
    assert "outcome deny" in body
    assert "outcome allow" in body


# ---------------------------------------------------------------------------
# The approval buttons
# ---------------------------------------------------------------------------

async def test_the_approve_button_resolves_a_waiting_act(client, identity):
    """The act is blocked on the queue; the button has to unblock it."""
    queue = approval_queue()
    queue.auto_resolver = None
    principal = identity.issue("safety_triage")

    async def press_approve() -> bool:
        for _ in range(400):
            pending = queue.pending()
            if pending:
                response = await client.post(f"/approvals/{pending[0].id}/approve")
                assert response.status_code == 200
                return True
            await asyncio.sleep(0.005)
        return False

    async def waiting_act():
        with acting_as(principal):
            return await guard_tool(principal, "update_case", {"case_id": "AE-0012"})

    pressed, result = await asyncio.gather(press_approve(), waiting_act())
    assert pressed
    assert result.display_outcome == "approved"
    assert result.allowed


async def test_the_reject_button_denies(client, identity):
    queue = approval_queue()
    queue.auto_resolver = None
    principal = identity.issue("safety_triage")

    async def press_reject() -> bool:
        for _ in range(400):
            pending = queue.pending()
            if pending:
                await client.post(f"/approvals/{pending[0].id}/reject")
                return True
            await asyncio.sleep(0.005)
        return False

    async def waiting_act():
        with acting_as(principal):
            return await guard_tool(principal, "update_case", {"case_id": "AE-0012"})

    pressed, result = await asyncio.gather(press_reject(), waiting_act())
    assert pressed
    assert result.display_outcome == "rejected"
    assert not result.allowed


async def test_answering_an_approval_that_is_gone_is_harmless(client):
    """Two people press the button, or somebody presses it after the timeout."""
    response = await client.post("/approvals/does-not-exist/approve")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Trace links
# ---------------------------------------------------------------------------

async def test_trace_links_point_at_the_configured_ui(client, monkeypatch):
    monkeypatch.setenv("TRACE_UI_URL", "http://localhost:18888")
    response = await client.get("/trace/abc123", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "http://localhost:18888/traces/detail/abc123"


async def test_trace_links_use_jaegers_url_shape_when_jaeger_is_configured(client, monkeypatch):
    monkeypatch.setenv("TRACE_UI_URL", "http://localhost:16686")
    response = await client.get("/trace/abc123", follow_redirects=False)
    assert response.headers["location"] == "http://localhost:16686/trace/abc123"


# ---------------------------------------------------------------------------
# The data behind the page
# ---------------------------------------------------------------------------

async def test_health_reports_the_two_switches(client):
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok"
    assert body["mode"] in ("mock", "live")
    assert body["engine"] in ("middleware", "hooks")


async def test_the_decisions_api_matches_the_audit_store(client, identity, isolated_audit_store):
    await seed_decisions(identity)
    rows = (await client.get("/api/decisions")).json()
    assert len(rows) == isolated_audit_store.count() == 3
    assert {row["agent"] for row in rows} == {"trial_ops", "shadow_agent"}
    for row in rows:
        assert row["rule_id"]
        assert row["reason"]
