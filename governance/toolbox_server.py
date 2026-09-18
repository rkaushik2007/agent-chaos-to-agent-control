"""The toolbox aggregator - one MCP endpoint in front of the tool servers.

This is a real MCP server, not a client-side convenience. Agents connect to it
and to nothing else. It authenticates every request from the bearer token the
caller presents, then answers `tools/list` with only the tools that caller is
registered to be granted, and refuses `tools/call` for anything it did not list.

It mirrors what a Microsoft Foundry toolbox does for you: one managed endpoint,
central credentials, and versions that can be promoted underneath running agents
without redeploying them. Building the local one on the same shape is what lets
`DEMO_MODE=live` swap in `FoundryToolbox` without any agent code changing.

Filtering here is server-side on purpose. A filter the client applies to itself
is a preference; a filter the endpoint applies is a control.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from hosting import serve_asgi
from governance.identity import IdentityError, IdentityProvider, Principal
from governance.registry import Registry


class ToolboxError(RuntimeError):
    """The toolbox refused a request."""


@dataclass(frozen=True)
class Promotion:
    """A version promotion, for the console and the narration."""

    previous: str
    current: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


class ToolboxAggregator:
    """One endpoint, many agents, a filtered view for each."""

    def __init__(
        self,
        upstream_url: str,
        registry: Registry,
        identity: IdentityProvider,
        *,
        version: str | None = None,
        name: str = "helix-toolbox",
    ) -> None:
        self._upstream_url = upstream_url
        self._registry = registry
        self._identity = identity
        self._version = version or registry.default_version
        self._name = name
        self._upstream: ClientSession | None = None
        self._upstream_tools: dict[str, types.Tool] = {}
        self._url: str | None = None
        self._ready = threading.Event()
        # Every refusal the endpoint itself made, for act 2's narration.
        self.refusals: list[tuple[str, str, str]] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("toolbox is not running")
        return self._url

    @property
    def version(self) -> str:
        return self._version

    @property
    def published_tools(self) -> tuple[str, ...]:
        return self._registry.tools_in_version(self._version)

    def promote(self, version: str) -> Promotion:
        """Make `version` the default the endpoint serves.

        No agent is restarted and no session is torn down. Connected agents pick
        the change up the next time they list tools - exactly like promoting a
        Foundry toolbox version with `toolboxes.update(default_version=...)`.
        """
        before = set(self.published_tools)
        previous = self._version
        after = set(self._registry.tools_in_version(version))  # raises on unknown version
        self._version = version
        return Promotion(
            previous=previous,
            current=version,
            added=tuple(sorted(after - before)),
            removed=tuple(sorted(before - after)),
        )

    async def _await_ready(self, timeout: float = 20.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._ready.is_set():
            if loop.time() > deadline:
                raise RuntimeError("toolbox never connected to its upstream tool server")
            await asyncio.sleep(0.01)

    def visible_to(self, principal: Principal | None) -> tuple[str, ...]:
        """The tool list this caller gets. Unauthenticated callers get nothing."""
        if principal is None:
            return ()
        return tuple(
            name for name in self.published_tools if name in principal.tool_scopes
        )

    # -- the MCP server ----------------------------------------------------

    def _principal(self, server: Server) -> Principal | None:
        try:
            request = getattr(server.request_context, "request", None)
        except LookupError:
            return None
        if request is None:
            return None
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        try:
            return self._identity.verify(header.split(" ", 1)[1].strip())
        except IdentityError:
            return None

    def _build_app(self) -> Starlette:
        server: Server = Server(self._name)

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            principal = self._principal(server)
            if principal is None:
                # No identity, no tools. An agent that cannot say who it is does
                # not get told what exists.
                self.refusals.append(("<unauthenticated>", "tools/list",
                                      "no verifiable identity was presented"))
                return []
            visible = self.visible_to(principal)
            return [self._upstream_tools[name] for name in visible
                    if name in self._upstream_tools]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> object:
            principal = self._principal(server)
            if principal is None:
                self.refusals.append(("<unauthenticated>", name,
                                      "no verifiable identity was presented"))
                raise ToolboxError(
                    f"toolbox: {name} requires an identity and none was presented"
                )
            visible = self.visible_to(principal)
            if name not in visible:
                self.refusals.append((principal.agent_id, name,
                                      "not published to this agent by the toolbox"))
                raise ToolboxError(
                    f"toolbox: {name} is not available to {principal.agent_id}"
                )
            assert self._upstream is not None
            result = await self._upstream.call_tool(name, arguments)
            if result.structuredContent is not None:
                return result.content, result.structuredContent
            return result.content

        manager = StreamableHTTPSessionManager(app=server, stateless=True)

        @contextlib.asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncIterator[None]:
            # The upstream MCP connection is opened HERE, inside the server's own
            # event loop, because `hosting.serve_asgi` runs each server on its own
            # loop in its own thread. A ClientSession created on the caller's loop
            # could not be awaited from a request handler running on this one.
            async with streamablehttp_client(self._upstream_url) as (read, write, _):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=30)
                ) as upstream:
                    await upstream.initialize()
                    listed = await upstream.list_tools()
                    self._upstream = upstream
                    self._upstream_tools = {t.name: t for t in listed.tools}
                    self._ready.set()
                    try:
                        async with manager.run():
                            yield
                    finally:
                        self._upstream = None

        async def handle(scope, receive, send):
            await manager.handle_request(scope, receive, send)

        return Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)


@contextlib.asynccontextmanager
async def run_aggregator(
    upstream_url: str,
    registry: Registry,
    identity: IdentityProvider,
    *,
    version: str | None = None,
    name: str = "helix-toolbox",
) -> AsyncIterator[ToolboxAggregator]:
    """Run the toolbox endpoint for the duration of the block."""
    aggregator = ToolboxAggregator(
        upstream_url, registry, identity, version=version, name=name
    )
    async with serve_asgi(aggregator._build_app()) as base_url:
        aggregator._url = f"{base_url}/mcp"
        # The endpoint is listening, but it is not useful until its lifespan has
        # connected upstream and learned the tool definitions it will re-publish.
        await aggregator._await_ready()
        try:
            yield aggregator
        finally:
            aggregator._url = None
