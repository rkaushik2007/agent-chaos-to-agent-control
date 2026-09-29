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
        from governance.credentials import async_credential

        credential = async_credential()

    _client = FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=credential,
    )
    return _client


# Azure Monitor is configured in `governance.telemetry.configure()`, as extra
# exporters on the one tracer provider. It used to be configured here, as a
# second provider, which OpenTelemetry silently refuses - see that function.


def reset() -> None:
    """Drop the cached client. Used by the tests."""
    global _client
    _client = None
