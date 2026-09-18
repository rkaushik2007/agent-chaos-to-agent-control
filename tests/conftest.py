"""Shared fixtures.

The governance layer keeps three things process-wide - the audit store, the
approval queue and the OpenTelemetry providers - because on stage there is one
of each. Tests must not inherit one another's, so every test gets a fresh audit
store and a drained queue.
"""

from __future__ import annotations

import os

import pytest

from governance import audit
from governance.approvals import approval_queue
from mcp_servers.clinical_tools import server as clinical_tools

# Telemetry is off for the suite. The acts turn it on; the tests assert on
# decisions, and an exporter trying to reach a collector that is not running
# only adds seconds.
os.environ.setdefault("ENABLE_INSTRUMENTATION", "false")
os.environ.setdefault("DEMO_MODE", "mock")


@pytest.fixture(autouse=True)
def isolated_audit_store(tmp_path):
    store = audit.AuditStore(tmp_path / "audit.db")
    audit.use_store(store)
    yield store
    store.close()
    audit.use_store(None)


@pytest.fixture(autouse=True)
def drained_approval_queue():
    queue = approval_queue()
    queue.auto_resolver = None
    queue.clear()
    yield queue
    queue.auto_resolver = None
    queue.clear()


@pytest.fixture(autouse=True)
def clean_tool_state():
    clinical_tools.reset_state()
    yield
    clinical_tools.reset_state()


@pytest.fixture
def fast_approvals(monkeypatch):
    """Make an unanswered approval time out in a fraction of a second."""
    monkeypatch.setenv("APPROVAL_TIMEOUT_SECONDS", "0.25")
    return 0.25
