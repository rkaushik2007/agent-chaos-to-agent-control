"""LIVE tool source: the Microsoft Foundry Toolbox (PREVIEW).

A Foundry toolbox is the managed version of what `toolbox_server.py` builds
locally: many tools behind one MCP-compatible endpoint, with central credentials
and versions you promote without redeploying the agents that consume them.

    https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/toolbox-overview

Two endpoints exist, and the difference matters:

    consumer   {project}/toolboxes/{name}/mcp?api-version=v1
               always serves `default_version` - what agents connect to
    developer  {project}/toolboxes/{name}/versions/{n}/mcp?api-version=v1
               pins one version - for testing before you promote it

This connects with `MCPStreamableHTTPTool` rather than with
`agent_framework_foundry_hosting.FoundryToolbox`, for two reasons, both of which
are worth saying out loud:

1. **Per-agent filtering.** `allowed_tools` on a client-opened MCP session gives
   each agent the same filtered view the local toolbox enforces server-side. A
   Foundry toolbox does not currently expose a per-consumer tool ACL, so this
   filter is client-side and is *not* the authoritative control - Foundry's own
   authentication and guardrails are. Act 2's point in LIVE is the single
   endpoint and the version promotion, both of which are genuinely Foundry's.
   # LIVE-TODO: move this filter to the toolbox if a per-agent ACL ships.
   # https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox

2. **Trace propagation.** Agent Framework injects W3C trace context into
   `tools/call` for MCP sessions the agent process opens itself, and cannot do
   so for a hosted connector, because there the service issues the request.
   Act 4's end-to-end trace therefore needs a client-opened session.
   https://learn.microsoft.com/en-us/agent-framework/agents/observability

`FoundryToolbox` remains the right choice for an agent *hosted inside* Foundry,
where the platform handles the connection and the call id. See
`docs/LIVE_SETUP.md`.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator

from agent_framework import MCPStreamableHTTPTool

from governance.identity import IdentityProvider, Principal
from governance.registry import Registry, registry
from governance.toolbox_server import Promotion

API_VERSION = "v1"

# A Foundry toolbox namespaces every tool it re-publishes with the label of the
# source it came from: `clinical_tools___search_docs`. Governance is written
# against the bare name - it is the tool that is classified, not the route to it
# - so the prefix is added when filtering and stripped before evaluation.
TOOL_NAMESPACE_SEPARATOR = "___"
DEFAULT_SERVER_LABEL = "clinical_tools"


def qualify(tool: str, server_label: str = DEFAULT_SERVER_LABEL) -> str:
    return f"{server_label}{TOOL_NAMESPACE_SEPARATOR}{tool}"


def strip_namespace(tool: str) -> str:
    """`clinical_tools___search_docs` -> `search_docs`. Idempotent."""
    _, separator, bare = tool.partition(TOOL_NAMESPACE_SEPARATOR)
    return bare if separator else tool


class FoundryToolboxError(RuntimeError):
    """LIVE toolbox configuration or control-plane failure."""


def consumer_endpoint(project_endpoint: str, toolbox_name: str) -> str:
    """The endpoint that always serves the toolbox's default version."""
    return (
        f"{project_endpoint.rstrip('/')}/toolboxes/{toolbox_name}"
        f"/mcp?api-version={API_VERSION}"
    )


def version_endpoint(project_endpoint: str, toolbox_name: str, version: str) -> str:
    """The endpoint pinned to one version, for testing before promoting."""
    return (
        f"{project_endpoint.rstrip('/')}/toolboxes/{toolbox_name}"
        f"/versions/{version}/mcp?api-version={API_VERSION}"
    )


def resolve_endpoint() -> str:
    """TOOLBOX_ENDPOINT if set, otherwise derived from the project and name."""
    explicit = os.getenv("TOOLBOX_ENDPOINT", "").strip()
    if explicit:
        return explicit
    project = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    name = os.getenv("TOOLBOX_NAME", "").strip()
    if not project or not name:
        raise FoundryToolboxError(
            "LIVE needs either TOOLBOX_ENDPOINT, or FOUNDRY_PROJECT_ENDPOINT and "
            "TOOLBOX_NAME together. See docs/LIVE_SETUP.md."
        )
    return consumer_endpoint(project, name)


class FoundryToolbox:
    """LIVE tool source. Same interface as `LocalToolbox`."""

    name = "foundry-toolbox"

    def __init__(
        self,
        endpoint: str,
        identity: IdentityProvider,
        reg: Registry | None = None,
        *,
        toolbox_name: str | None = None,
        project_endpoint: str | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._identity = identity
        self._registry = reg or registry()
        self._toolbox_name = toolbox_name or os.getenv("TOOLBOX_NAME", "").strip()
        self._project_endpoint = (
            project_endpoint or os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        )
        self._version = "default"
        self._catalogue_version = self._registry.default_version
        self.refusals: list[tuple[str, str, str]] = []

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def server_label(self) -> str:
        """The namespace Foundry prefixes onto tools from this version's source.

        Per catalogue version, because Foundry caches tool discovery per label -
        see the note in `infra/create_toolbox.py`.
        """
        explicit = os.getenv("TOOLBOX_SERVER_LABEL", "").strip()
        if explicit:
            return explicit
        prefix = os.getenv("TOOLBOX_SERVER_LABEL_PREFIX", DEFAULT_SERVER_LABEL).strip()
        return f"{prefix}_{self._catalogue_version}"

    @property
    def version(self) -> str:
        return self._version

    @property
    def published_tools(self) -> tuple[str, ...]:
        """What the catalogue says this version publishes.

        The authoritative list lives in Foundry; this is the locally catalogued
        view used for filtering and narration, and `session_for` intersects it
        with whatever the endpoint actually returns.
        """
        try:
            return self._registry.tools_in_version(self._catalogue_version)
        except KeyError:  # pragma: no cover - defensive
            return tuple(self._registry.tools)

    def visible_to(self, principal: Principal) -> tuple[str, ...]:
        record = self._registry.agent(principal.agent_id)
        if record is None:
            return ()
        return tuple(t for t in self.published_tools if t in record.allowed_tools)

    @contextlib.asynccontextmanager
    async def session_for(self, principal: Principal) -> AsyncIterator[MCPStreamableHTTPTool]:
        allowed = self.visible_to(principal)
        if not allowed:
            self.refusals.append(
                (principal.agent_id, "tools/list", "no tools are granted to this agent")
            )
        tool = MCPStreamableHTTPTool(
            name=self._toolbox_name or "foundry-toolbox",
            url=self._endpoint,
            description="The Microsoft Foundry toolbox endpoint for Helix clinical tools.",
            # Re-read per request so an expiring Entra token is refreshed rather
            # than reused until it fails.
            header_provider=lambda _ctx: self._identity.bearer_headers(principal),
            # Foundry publishes namespaced names, so the filter must ask for
            # namespaced names.
            allowed_tools=[qualify(t, self.server_label) for t in allowed] or None,
            request_timeout=60,
        )
        async with tool:
            yield tool

    def read_default_version(self) -> str:
        """Ask the control plane which version the consumer endpoint serves."""
        if not (self._project_endpoint and self._toolbox_name):
            return self._version
        try:
            from azure.ai.projects import AIProjectClient

            with AIProjectClient(
                endpoint=self._project_endpoint,
                credential=self._identity.credential(),  # type: ignore[attr-defined]
            ) as client:
                toolbox = client.toolboxes.get(name=self._toolbox_name)
            self._version = str(getattr(toolbox, "default_version", self._version))
            for name, live in self._registry.live_versions.items():
                if live == self._version:
                    self._catalogue_version = name
                    break
        except Exception:  # noqa: BLE001 - narration only, never fatal
            pass
        return self._version

    def promote(self, version: str) -> Promotion:
        """Promote a toolbox version to default, for real.

        `toolboxes.update(name=..., default_version=...)` is the documented
        control-plane call, and the consumer endpoint starts serving the new
        version immediately - no agent is redeployed.
        """
        if not (self._project_endpoint and self._toolbox_name):
            raise FoundryToolboxError(
                "promoting a version needs FOUNDRY_PROJECT_ENDPOINT and TOOLBOX_NAME"
            )
        from azure.ai.projects import AIProjectClient

        # Act 2 asks for the catalogue name ("v2"); Foundry wants the version
        # id it assigned ("2"). config/toolbox.yaml records the mapping.
        live_version = self._registry.live_version_of(version)
        before = set(self.published_tools)
        previous = self._version
        with AIProjectClient(
            endpoint=self._project_endpoint,
            credential=self._identity.credential(),  # type: ignore[attr-defined]
        ) as project_client:
            toolbox = project_client.toolboxes.update(
                name=self._toolbox_name, default_version=live_version
            )
        self._version = getattr(toolbox, "default_version", live_version)
        self._catalogue_version = version
        after = set(self._registry.tools_in_version(version))
        return Promotion(
            previous=previous,
            current=self._version,
            added=tuple(sorted(after - before)),
            removed=tuple(sorted(before - after)),
        )


@contextlib.asynccontextmanager
async def foundry_toolbox(
    upstream_url: str | None,
    identity: IdentityProvider,
    reg: Registry | None = None,
    *,
    version: str | None = None,
) -> AsyncIterator[FoundryToolbox]:
    """LIVE counterpart of `governance.toolbox.local_toolbox`.

    `upstream_url` is ignored: in LIVE there is no local tool server to front,
    because Foundry is already fronting the tools. The parameter is kept so the
    acts can call one factory in both modes.
    """
    reg = reg or registry()
    endpoint = resolve_endpoint()
    toolbox = FoundryToolbox(endpoint, identity, reg)

    if version:
        # A caller naming a version wants that version specifically, so use the
        # version-pinned developer endpoint. The name is this repo's catalogue
        # name ("v2"); Foundry wants the id it assigned ("2").
        project = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        name = os.getenv("TOOLBOX_NAME", "").strip()
        if project and name:
            toolbox._endpoint = version_endpoint(
                project, name, reg.live_version_of(version)
            )
            toolbox._version = reg.live_version_of(version)
            toolbox._catalogue_version = version
    else:
        toolbox.read_default_version()
    yield toolbox
