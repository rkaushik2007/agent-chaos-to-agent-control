"""Agent identity.

One interface, two implementations. Everything downstream - the toolbox, the
enforcement seam, the A2A gateway, the audit store, the spans - consumes a
`Principal` and never learns which provider minted it. That is what makes the
MOCK and LIVE governance code identical rather than merely similar.

MOCK issues locally signed, short-lived tokens carrying the agent id and its
scopes. LIVE resolves the Entra agent identity Foundry provisioned. See
`EntraAgentIdentityProvider` for an honest account of what LIVE can and cannot
do today.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from governance.registry import Registry, registry

DEFAULT_TTL_SECONDS = 300


class IdentityError(RuntimeError):
    """Raised when an agent cannot be given an identity, or presents a bad one."""


@dataclass(frozen=True)
class Principal:
    """Who is acting. The unit of attribution for every decision in the demo."""

    agent_id: str
    # `entra.agent_id` on spans and audit rows. In LIVE this is the Entra agent
    # identity object id; in MOCK it is `mock:<agent_id>`, which is visibly not
    # an Entra id so nobody can mistake a rehearsal trace for a real one.
    identity_label: str
    issuer: str
    scopes: frozenset[str] = frozenset()
    token: str = field(default="", repr=False)
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at

    @property
    def tool_scopes(self) -> frozenset[str]:
        return frozenset(s.removeprefix("tool:") for s in self.scopes if s.startswith("tool:"))

    @property
    def peer_scopes(self) -> frozenset[str]:
        return frozenset(s.removeprefix("peer:") for s in self.scopes if s.startswith("peer:"))

    def __str__(self) -> str:
        return f"{self.agent_id} ({self.identity_label})"


@runtime_checkable
class IdentityProvider(Protocol):
    """Issues and verifies agent identities."""

    name: str

    def issue(self, agent_id: str) -> Principal:
        """Mint an identity for a registered agent.

        Raises `IdentityError` for an agent that is not in the registry. An
        unregistered agent does not get a weaker identity - it gets none, and so
        never reaches the toolbox at all.
        """

    def verify(self, token: str) -> Principal:
        """Recover the principal a token asserts, or raise `IdentityError`."""

    def bearer_headers(self, principal: Principal) -> dict[str, str]:
        """Headers to present when calling the tool endpoint as this principal."""


def _scopes_for(reg: Registry, agent_id: str) -> frozenset[str]:
    record = reg.require_agent(agent_id)
    return frozenset(
        [f"tool:{t}" for t in sorted(record.allowed_tools)]
        + [f"peer:{p}" for p in sorted(record.allowed_peers)]
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class MockIdentityProvider:
    """Locally signed, short-lived tokens. No network, no Azure, no secrets on disk.

    The signing key is generated per process unless `MOCK_IDENTITY_KEY` is set,
    so a token from one rehearsal cannot be replayed into the next.
    """

    name = "mock"
    issuer = "helix-governance/mock"

    def __init__(
        self,
        reg: Registry | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        key: bytes | None = None,
    ) -> None:
        self._registry = reg or registry()
        self._ttl = ttl_seconds
        env_key = os.getenv("MOCK_IDENTITY_KEY")
        self._key = key or (env_key.encode() if env_key else secrets.token_bytes(32))

    def issue(self, agent_id: str) -> Principal:
        if not self._registry.is_registered(agent_id):
            raise IdentityError(
                f"{agent_id} is not in the agent registry, so it cannot be issued an identity"
            )
        record = self._registry.require_agent(agent_id)
        scopes = _scopes_for(self._registry, agent_id)
        now = time.time()
        expires_at = now + self._ttl
        payload = {
            "iss": self.issuer,
            "sub": agent_id,
            "aid": record.identity_label,
            "scp": sorted(scopes),
            "iat": int(now),
            "exp": int(expires_at),
        }
        return Principal(
            agent_id=agent_id,
            identity_label=record.identity_label,
            issuer=self.issuer,
            scopes=scopes,
            token=self._sign(payload),
            expires_at=expires_at,
        )

    def verify(self, token: str) -> Principal:
        try:
            body, signature = token.rsplit(".", 1)
        except ValueError:
            raise IdentityError("malformed token") from None
        expected = _b64(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            raise IdentityError("token signature does not verify")
        payload = json.loads(_unb64(body))
        if time.time() >= payload["exp"]:
            raise IdentityError(f"identity for {payload['sub']} expired")
        return Principal(
            agent_id=payload["sub"],
            identity_label=payload["aid"],
            issuer=payload["iss"],
            scopes=frozenset(payload["scp"]),
            token=token,
            expires_at=float(payload["exp"]),
        )

    def bearer_headers(self, principal: Principal) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {principal.token}",
            "x-helix-agent-id": principal.agent_id,
        }

    def _sign(self, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}"


def identity_provider(reg: Registry | None = None) -> IdentityProvider:
    """The identity provider for the current `DEMO_MODE`."""
    from governance import settings

    if settings.demo_mode() == "live":
        from governance.identity_live import EntraAgentIdentityProvider

        return EntraAgentIdentityProvider(reg or registry())
    return MockIdentityProvider(reg or registry())
