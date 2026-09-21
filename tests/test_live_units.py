"""Milestone 7: the parts of LIVE that can be tested without Azure.

The LIVE paths were verified by hand against a real Foundry project (see
docs/LIVE_SETUP.md). These tests cover the pieces that are pure - endpoint
shapes, the tool namespace, configuration errors - so a refactor cannot quietly
break them between conferences, when nobody has a project to hand.
"""

from __future__ import annotations

import pytest

from governance.identity import IdentityError
from governance.identity_live import FOUNDRY_SCOPE, EntraAgentIdentityProvider
from governance.model_live import FoundryConfigurationError, foundry_chat_client
from governance.model_live import reset as reset_model_client
from governance.registry import registry
from governance.toolbox_live import (
    FoundryToolboxError,
    consumer_endpoint,
    qualify,
    resolve_endpoint,
    strip_namespace,
    version_endpoint,
)

PROJECT = "https://acct.services.ai.azure.com/api/projects/proj"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_the_consumer_endpoint_has_no_version_in_it():
    """It always serves default_version - that is what makes promotion work."""
    url = consumer_endpoint(PROJECT, "helix")
    assert url == f"{PROJECT}/toolboxes/helix/mcp?api-version=v1"
    assert "/versions/" not in url


def test_the_developer_endpoint_pins_a_version():
    assert version_endpoint(PROJECT, "helix", "2") == (
        f"{PROJECT}/toolboxes/helix/versions/2/mcp?api-version=v1"
    )


def test_a_trailing_slash_on_the_project_endpoint_is_tolerated():
    assert consumer_endpoint(PROJECT + "/", "helix") == consumer_endpoint(PROJECT, "helix")


def test_an_explicit_toolbox_endpoint_wins(monkeypatch):
    monkeypatch.setenv("TOOLBOX_ENDPOINT", "https://pinned.example/mcp")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", PROJECT)
    monkeypatch.setenv("TOOLBOX_NAME", "helix")
    assert resolve_endpoint() == "https://pinned.example/mcp"


def test_the_endpoint_is_derived_when_not_pinned(monkeypatch):
    monkeypatch.setenv("TOOLBOX_ENDPOINT", "")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", PROJECT)
    monkeypatch.setenv("TOOLBOX_NAME", "helix")
    assert resolve_endpoint() == consumer_endpoint(PROJECT, "helix")


def test_missing_toolbox_configuration_says_what_is_missing(monkeypatch):
    for var in ("TOOLBOX_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT", "TOOLBOX_NAME"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(FoundryToolboxError, match="TOOLBOX_ENDPOINT"):
        resolve_endpoint()


# ---------------------------------------------------------------------------
# The tool namespace
# ---------------------------------------------------------------------------

def test_foundry_namespacing_round_trips():
    """Foundry publishes `<server_label>___<tool>`; policy sees the bare name."""
    assert qualify("search_docs", "clinical_tools_v1") == "clinical_tools_v1___search_docs"
    assert strip_namespace("clinical_tools_v1___search_docs") == "search_docs"


def test_stripping_a_bare_name_is_a_no_op():
    """MOCK never namespaces, and the same code path handles both."""
    assert strip_namespace("search_docs") == "search_docs"
    assert strip_namespace(strip_namespace(qualify("read_case"))) == "read_case"


def test_a_namespaced_tool_is_evaluated_as_its_bare_name():
    """The regression this guards: every policy lookup missing in LIVE."""
    from governance.policy import Policy
    from governance.registry import policy_input_for

    reg, pol = registry(), Policy.load()
    bare = pol.evaluate(policy_input_for(reg, "trial_ops", "search_docs"))
    namespaced = pol.evaluate(
        policy_input_for(reg, "trial_ops", strip_namespace(qualify("search_docs")))
    )
    assert bare == namespaced
    assert bare.decision == "allow"


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def test_catalogue_versions_map_to_foundry_version_ids():
    """Act 2 promotes "v2"; Foundry wants the id it assigned."""
    reg = registry()
    assert reg.live_version_of("v1") == "1"
    assert reg.live_version_of("v2") == "2"


def test_an_unmapped_version_falls_back_to_its_own_name():
    assert registry().live_version_of("v99") == "v99"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_live_identity_still_refuses_an_unregistered_agent():
    provider = EntraAgentIdentityProvider(registry(), credential=object())
    with pytest.raises(IdentityError, match="not in the agent registry"):
        provider.issue("shadow_agent")


def test_live_identity_refuses_to_start_without_a_configured_agent_identity():
    """An `entra.agent_id` of None on every span is worse than a clear failure."""
    provider = EntraAgentIdentityProvider(
        registry(), credential=object(), require_agent_identity=True
    )
    with pytest.raises(IdentityError, match="no Entra agent identity configured"):
        provider.issue("trial_ops")


def test_live_identity_never_fakes_a_token():
    """`verify` is not implemented, and deliberately not stubbed to succeed."""
    provider = EntraAgentIdentityProvider(registry(), credential=object())
    with pytest.raises(IdentityError, match="does not verify tokens"):
        provider.verify("anything")


def test_the_foundry_scope_is_the_documented_one():
    assert FOUNDRY_SCOPE == "https://ai.azure.com/.default"


# ---------------------------------------------------------------------------
# Model client
# ---------------------------------------------------------------------------

def test_live_without_configuration_says_exactly_what_is_missing(monkeypatch):
    reset_model_client()
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", raising=False)
    with pytest.raises(FoundryConfigurationError) as excinfo:
        foundry_chat_client()
    message = str(excinfo.value)
    assert "FOUNDRY_PROJECT_ENDPOINT" in message
    assert "AZURE_AI_MODEL_DEPLOYMENT_NAME" in message
    # The error has to point at the way out, not just the problem.
    assert "DEMO_MODE=mock" in message
    reset_model_client()


# ---------------------------------------------------------------------------
# Mode switching
# ---------------------------------------------------------------------------

def test_the_mode_switch_selects_the_live_implementations(monkeypatch):
    from governance.identity import identity_provider
    from governance.toolbox import tool_source_factory
    from governance.toolbox_live import foundry_toolbox

    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("REQUIRE_AGENT_IDENTITY", "false")
    assert isinstance(identity_provider(registry()), EntraAgentIdentityProvider)
    assert tool_source_factory() is foundry_toolbox

    monkeypatch.setenv("DEMO_MODE", "mock")
    from governance.identity import MockIdentityProvider
    from governance.toolbox import local_toolbox

    assert isinstance(identity_provider(registry()), MockIdentityProvider)
    assert tool_source_factory() is local_toolbox


# ---------------------------------------------------------------------------
# Telemetry: one provider, and MOCK stays offline
# ---------------------------------------------------------------------------

def test_mock_never_looks_up_application_insights(monkeypatch):
    """MOCK is offline by promise. It must not even ask Azure."""
    from governance import telemetry

    constructed = []

    class Spy:
        def __init__(self, *args, **kwargs):
            constructed.append(1)

    monkeypatch.setenv("DEMO_MODE", "mock")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=would-leak")
    monkeypatch.setattr("azure.ai.projects.AIProjectClient", Spy)

    assert telemetry._application_insights_connection_string() is None
    assert constructed == []


def test_live_prefers_an_explicit_connection_string(monkeypatch):
    from governance import telemetry

    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=explicit")
    assert telemetry._application_insights_connection_string() == "InstrumentationKey=explicit"


def _fake_provider(*exporter_names: str):
    """A provider shaped like the SDK's, holding exporters with these class names."""
    from types import SimpleNamespace

    processors = tuple(
        SimpleNamespace(span_exporter=type(name, (), {})()) for name in exporter_names
    )
    return SimpleNamespace(
        _active_span_processor=SimpleNamespace(_span_processors=processors)
    )


def test_azure_monitor_is_not_reported_when_only_the_local_exporter_is_attached(monkeypatch):
    """The bug this guards: "a span processor exists" was taken to mean "Azure
    is receiving spans". The local OTLP exporter satisfied that check on its own,
    so the act announced Azure export that was not happening."""
    from opentelemetry import trace

    from governance import telemetry

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: _fake_provider("OTLPSpanExporter"))
    assert telemetry.azure_monitor_active() is False


def test_azure_monitor_is_reported_when_its_exporter_is_attached(monkeypatch):
    from opentelemetry import trace

    from governance import telemetry

    monkeypatch.setattr(
        trace, "get_tracer_provider",
        lambda: _fake_provider("OTLPSpanExporter", "AzureMonitorTraceExporter"),
    )
    assert telemetry.azure_monitor_active() is True


def test_azure_monitor_exporters_cover_traces_logs_and_metrics():
    from governance import telemetry

    kinds = {
        type(e).__name__
        for e in telemetry._azure_monitor_exporters(
            "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
            "IngestionEndpoint=https://example.invalid/"
        )
    }
    assert kinds == {
        "AzureMonitorTraceExporter",
        "AzureMonitorLogExporter",
        "AzureMonitorMetricExporter",
    }
