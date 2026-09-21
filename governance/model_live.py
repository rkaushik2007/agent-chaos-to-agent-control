"""LIVE model client: a Microsoft Foundry model deployment.

The only thing `DEMO_MODE=live` changes about how an agent thinks. The
middleware list, the tools, the principal and every governance decision are
constructed identically in both modes - see `agents/runner.py`.

Worth saying on stage: in LIVE the model chooses the tool, so the demo is no
longer scripted. The policy still refuses what it refused before, because the
policy never consulted the planner. That is the argument for putting governance
at the tool seam rather than in the prompt.
"""

from __future__ import annotations

import os

_client = None


class FoundryConfigurationError(RuntimeError):
    """LIVE is selected but the Foundry configuration is incomplete."""


def foundry_chat_client(credential=None):
    """A `FoundryChatClient` for the configured project and deployment.

    Cached per process: the client holds a credential and an HTTP pool, and
    re-creating it for every agent run would re-authenticate on each act.
    """
    global _client
    if _client is not None:
        return _client

    project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    model = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "").strip()
    missing = [
        name for name, value in (
            ("FOUNDRY_PROJECT_ENDPOINT", project_endpoint),
            ("AZURE_AI_MODEL_DEPLOYMENT_NAME", model),
        ) if not value
    ]
    if missing:
        raise FoundryConfigurationError(
            f"DEMO_MODE=live needs {', '.join(missing)}. See docs/LIVE_SETUP.md. "
            "Run with DEMO_MODE=mock to present without Azure."
        )

    from agent_framework.foundry import FoundryChatClient

    if credential is None:
        from azure.identity.aio import AzureCliCredential, DefaultAzureCredential

        credential = (
            AzureCliCredential()
            if os.getenv("AZURE_USE_CLI_CREDENTIAL", "true").strip().lower()
            in ("true", "1", "yes", "on")
            else DefaultAzureCredential()
        )

    _client = FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=credential,
    )
    return _client


async def configure_azure_monitor_from_project() -> bool:
    """Send telemetry to the Foundry project's own Application Insights.

    `FoundryChatClient.configure_azure_monitor()` reads the connection string
    from the project, so nothing has to be copied into `.env`. If the project
    has no Application Insights attached, this reports failure rather than
    raising - LIVE should still run, with traces going only to the local
    collector.

    https://learn.microsoft.com/en-us/agent-framework/agents/observability
    """
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if connection_string:
        from azure.monitor.opentelemetry import configure_azure_monitor

        from agent_framework.observability import create_resource, enable_instrumentation

        configure_azure_monitor(
            connection_string=connection_string,
            resource=create_resource(),
            enable_live_metrics=True,
        )
        enable_instrumentation()
        return True

    # `FoundryChatClient.configure_azure_monitor()` does not raise when the
    # project has no Application Insights attached - it logs and installs
    # nothing. Returning True on "no exception" therefore reported success while
    # exporting precisely zero spans, which is the worst possible answer: the
    # act says telemetry is flowing to Azure and it is not. Ask the project for
    # a connection string first, and only claim success if one exists.
    try:
        client = foundry_chat_client()
        if not await _project_has_application_insights():
            return False
        await client.configure_azure_monitor(enable_live_metrics=True)
    except Exception:  # noqa: BLE001 - reported by the caller, never fatal
        return False
    return _azure_monitor_is_exporting()


async def _project_has_application_insights() -> bool:
    """Does the Foundry project have an Application Insights resource attached?"""
    import os

    from azure.ai.projects.aio import AIProjectClient
    from azure.identity.aio import AzureCliCredential

    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        return False
    try:
        async with AzureCliCredential() as credential:
            async with AIProjectClient(endpoint=endpoint, credential=credential) as client:
                connection_string = (
                    await client.telemetry.get_application_insights_connection_string()
                )
        return bool(connection_string)
    except Exception:  # noqa: BLE001 - absence is the common case, not an error
        return False


def _azure_monitor_is_exporting() -> bool:
    """Did a span processor actually get installed?

    The only honest check. Anything else is trusting a call that stays quiet
    when it does nothing.
    """
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    active = getattr(provider, "_active_span_processor", None)
    return bool(getattr(active, "_span_processors", ()))


def reset() -> None:
    """Drop the cached client. Used by the tests."""
    global _client
    _client = None
