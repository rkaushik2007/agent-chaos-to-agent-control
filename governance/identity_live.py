"""LIVE identity: Microsoft Entra agent identities.

Read this before demoing it, because there is an important limitation and the
talk should state it rather than imply otherwise.

**There is no client-side API to acquire a token *as* an Entra agent identity.**
Microsoft Foundry provisions an agent identity blueprint and agent identities
for your project, and the blueprint -> agent identity -> scoped token exchange
is performed by Agent Service at tool-call time. The documentation is explicit:
"Developers don't manage tokens directly - Agent Service handles the entire
exchange."

    https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-identity

So an agent running locally, as this demo's agents do, cannot mint a token whose
subject is its agent identity. What it can do - and what this provider does - is
two things that are both real and both useful:

1. **Carry the agent identity as the governance claim.** The `agentIdentityId`
   Foundry provisioned is configured per agent in `config/agents.yaml` and is
   stamped on every span (`entra.agent_id`) and every audit row. That is the
   identifier an Entra administrator applies Conditional Access to, assigns RBAC
   to, and searches by in the Entra admin center, so attributing decisions to it
   is exactly right.

2. **Obtain a real Entra token for the resource being called**, via
   `azure-identity`, so the Foundry Toolbox endpoint genuinely authenticates the
   caller. That token's subject is whatever principal you signed in as - your
   user in development, a managed identity in a deployment - not the agent
   identity.

What that means on stage: say "this is the agent's identity, and here is the
token we present to the toolbox". Do not say "this token is issued to the agent
identity", because today it is not.

# LIVE-TODO: if a client-side agent-identity credential ships, replace
# `_resource_token` with it. Watch:
#   https://learn.microsoft.com/en-us/entra/agent-id/agent-identities
#   https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-identity
"""

from __future__ import annotations

import os
import threading
import time

from governance.identity import IdentityError, Principal, _scopes_for
from governance.registry import Registry, registry

# The audience the Foundry data plane, including a toolbox endpoint, expects.
FOUNDRY_SCOPE = "https://ai.azure.com/.default"

# Refresh a little before expiry rather than at it.
REFRESH_MARGIN_SECONDS = 300


class EntraAgentIdentityProvider:
    """Resolves Entra agent identities and Foundry data-plane tokens."""

    name = "entra"
    issuer = "https://login.microsoftonline.com"

    def __init__(
        self,
        reg: Registry | None = None,
        *,
        credential=None,
        scope: str = FOUNDRY_SCOPE,
        require_agent_identity: bool | None = None,
    ) -> None:
        self._registry = reg or registry()
        self._scope = scope
        self._credential = credential
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None
        # Refusing to start without configured agent identities is the safe
        # default for a governance demo: an `entra.agent_id` of None on every
        # span is worse than a clear failure at startup.
        if require_agent_identity is None:
            require_agent_identity = os.getenv(
                "REQUIRE_AGENT_IDENTITY", "true"
            ).strip().lower() not in ("false", "0", "no", "off")
        self._require_agent_identity = require_agent_identity

    # -- credential --------------------------------------------------------

    def credential(self):
        if self._credential is None:
            from azure.identity import AzureCliCredential, DefaultAzureCredential

            # AzureCliCredential when a developer is signed in with `az login`,
            # which is the demo case and avoids DefaultAzureCredential probing
            # every source in turn while an audience watches.
            if os.getenv("AZURE_USE_CLI_CREDENTIAL", "true").strip().lower() in (
                "true", "1", "yes", "on",
            ):
                self._credential = AzureCliCredential()
            else:
                self._credential = DefaultAzureCredential()
        return self._credential

    def _resource_token(self) -> tuple[str, float]:
        """A real Entra token for the resource. Subject: the signed-in principal."""
        with self._lock:
            if self._token is not None:
                token, expires_at = self._token
                if time.time() < expires_at - REFRESH_MARGIN_SECONDS:
                    return self._token
            try:
                access = self.credential().get_token(self._scope)
            except Exception as exc:  # noqa: BLE001 - surfaced with context
                raise IdentityError(
                    f"could not obtain an Entra token for {self._scope}: "
                    f"{type(exc).__name__}: {exc}. Run `az login`."
                ) from exc
            self._token = (access.token, float(access.expires_on))
            return self._token

    # -- IdentityProvider --------------------------------------------------

    def issue(self, agent_id: str) -> Principal:
        if not self._registry.is_registered(agent_id):
            raise IdentityError(
                f"{agent_id} is not in the agent registry, so it cannot be issued an identity"
            )
        record = self._registry.require_agent(agent_id)
        if not record.entra_agent_id and self._require_agent_identity:
            raise IdentityError(
                f"{agent_id} has no Entra agent identity configured. Set "
                f"{agent_id.upper()}_AGENT_IDENTITY_ID in .env - see docs/LIVE_SETUP.md. "
                "Set REQUIRE_AGENT_IDENTITY=false to run without one."
            )

        token, expires_at = self._resource_token()
        return Principal(
            agent_id=agent_id,
            # The Entra agent identity object id when configured. Never silently
            # a mock label in LIVE - that would put a fake id on a real trace.
            identity_label=record.entra_agent_id or f"unconfigured:{agent_id}",
            issuer=self.issuer,
            scopes=_scopes_for(self._registry, agent_id),
            token=token,
            expires_at=expires_at,
        )

    def verify(self, token: str) -> Principal:
        """Not implemented, and deliberately not faked.

        In LIVE the toolbox endpoint is Foundry's, and Foundry validates the
        bearer token. Nothing in this repo is the resource server for a real
        Entra token, so there is nothing here that could honestly verify one.
        """
        raise IdentityError(
            "EntraAgentIdentityProvider does not verify tokens. In LIVE the "
            "Foundry toolbox endpoint is the resource server and validates them."
        )

    def bearer_headers(self, principal: Principal) -> dict[str, str]:
        token, _ = self._resource_token()
        return {
            "Authorization": f"Bearer {token}",
            # Not a security control - Foundry authenticates the bearer token.
            # It is there so a request can be correlated to an agent in logs.
            "x-helix-agent-id": principal.agent_id,
        }
