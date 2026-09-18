"""The tool source.

`ToolSource` is the seam between the agents and wherever the tools actually
live. In MOCK that is `LocalToolbox`, the aggregator in `toolbox_server.py` in
front of the local `clinical-tools` MCP server. In LIVE it is `FoundryToolbox`,
the managed Microsoft Foundry toolbox endpoint.

Agents hold a `ToolSource` and nothing else. They never learn which one they
have, which is why promoting a toolbox version - locally or in Foundry - needs
no change on their side.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from agent_framework import MCPStreamableHTTPTool

from governance.identity import IdentityProvider, Principal
from governance.registry import Registry, registry
from governance.toolbox_server import (
    Promotion,
    ToolboxAggregator,
    ToolboxError,
    run_aggregator,
)

__all__ = [
    "ToolSource",
    "LocalToolbox",
    "ToolboxError",
    "Promotion",
    "tool_source",
]


@runtime_checkable
class ToolSource(Protocol):
    """One endpoint, whatever is behind it."""

    name: str

    @property
    def endpoint(self) -> str:
        """The single URL agents connect to."""

    @property
    def version(self) -> str:
        """The version this endpoint currently serves."""

    def visible_to(self, principal: Principal) -> tuple[str, ...]:
        """The tools this caller will be shown."""

    def session_for(self, principal: Principal): ...

    def promote(self, version: str) -> Promotion:
        """Make another version the default, without restarting any agent."""


class LocalToolbox:
    """MOCK tool source: the local aggregator over the clinical-tools server."""

    name = "local-toolbox"

    def __init__(
        self,
        aggregator: ToolboxAggregator,
        identity: IdentityProvider,
        reg: Registry | None = None,
    ) -> None:
        self._aggregator = aggregator
        self._identity = identity
        self._registry = reg or registry()

    @property
    def endpoint(self) -> str:
        return self._aggregator.url

    @property
    def version(self) -> str:
        return self._aggregator.version

    @property
    def published_tools(self) -> tuple[str, ...]:
        return self._aggregator.published_tools

    @property
    def refusals(self) -> list[tuple[str, str, str]]:
        return self._aggregator.refusals

    def visible_to(self, principal: Principal) -> tuple[str, ...]:
        return self._aggregator.visible_to(principal)

    def promote(self, version: str) -> Promotion:
        return self._aggregator.promote(version)

    @contextlib.asynccontextmanager
    async def session_for(self, principal: Principal) -> AsyncIterator[MCPStreamableHTTPTool]:
        """Open this agent's view of the toolbox.

        The bearer token goes on every request, so the endpoint - not the client -
        decides what comes back. `header_provider` is re-read per request, so a
        token that expires mid-run is not silently reused.
        """
        tool = MCPStreamableHTTPTool(
            name=self._aggregator._name,
            url=self.endpoint,
            description="The single governed endpoint for Helix clinical tools.",
            header_provider=lambda _ctx: self._identity.bearer_headers(principal),
            request_timeout=30,
        )
        async with tool:
            yield tool


@contextlib.asynccontextmanager
async def local_toolbox(
    upstream_url: str,
    identity: IdentityProvider,
    reg: Registry | None = None,
    *,
    version: str | None = None,
) -> AsyncIterator[LocalToolbox]:
    reg = reg or registry()
    async with run_aggregator(upstream_url, reg, identity, version=version) as agg:
        yield LocalToolbox(agg, identity, reg)


def tool_source_factory():
    """Which tool source `DEMO_MODE` selects.

    Returns an async context manager factory rather than an instance, because
    both toolboxes hold a live connection for the length of a run.
    """
    from governance import settings

    if settings.demo_mode() == "live":
        from governance.toolbox_live import foundry_toolbox

        return foundry_toolbox
    return local_toolbox
