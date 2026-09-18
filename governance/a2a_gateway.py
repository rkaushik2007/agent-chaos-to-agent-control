"""The A2A gateway.

Agent-to-agent delegation is where an agent stops being a contained problem. A
tool call reaches a system you control; a delegation reaches somebody else's
agent, with your data, under your name, and whatever it does next is attributed
to you.

So every outbound hop passes four independent checks:

    A2A-CARD-000    the peer's agent card resolves and is allow-listed
    A2A-GRANT-000   this caller's identity may delegate to this peer
    A2A-DEPTH-000   the delegation chain is within the caller's limit
    A2A-EGRESS-000  this payload's classification may leave for this peer

They are independent on purpose - each is a different failure the others cannot
catch - and each is tested on its own in `tests/test_a2a_gateway.py`.

Agent Framework's A2A support is beta. It is wrapped here rather than used
directly by the acts, so the beta surface sits behind one interface: if
`A2AAgent`'s shape changes, this file changes and nothing else does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from governance import telemetry
from governance.context import acting_as, delegation_chain, delegation_depth
from governance.enforce import A2A, GuardResult, apply_decision
from governance.identity import IdentityProvider, Principal
from governance.policy import ALLOW, DENY, Decision
from governance.registry import Registry, registry

CHECK_CARD = "A2A-CARD-000"
CHECK_GRANT = "A2A-GRANT-000"
CHECK_DEPTH = "A2A-DEPTH-000"
CHECK_EGRESS = "A2A-EGRESS-000"

ALLOWED = Decision(ALLOW, "A2A-000", "all four gateway checks passed")

# Propagated so the receiving side knows how far it already is from the
# originating request, and cannot be talked into starting its own chain afresh.
DEPTH_HEADER = "x-helix-delegation-depth"
CHAIN_HEADER = "x-helix-delegation-chain"
CALLER_HEADER = "x-helix-calling-agent"


@dataclass(frozen=True)
class AgentCardInfo:
    """The little of a peer's card the gateway makes decisions about."""

    name: str
    url: str
    organisation: str = ""
    resolved: bool = True

    @classmethod
    def unresolvable(cls, url: str) -> AgentCardInfo:
        return cls(name="", url=url, resolved=False)


@dataclass
class Delegation:
    """The outcome of one attempted hop."""

    result: GuardResult
    reply: str | None = None

    @property
    def allowed(self) -> bool:
        return self.result.allowed


# --------------------------------------------------------------------------
# The four checks. Each is a pure function returning a refusal, or None.
# --------------------------------------------------------------------------

def check_card(reg: Registry, peer_id: str, card: AgentCardInfo | None) -> Decision | None:
    peer = reg.peer(peer_id)
    if peer is None:
        return Decision(DENY, CHECK_CARD,
                        f"{peer_id} is not a peer Helix has ever registered")
    if not peer.allow_listed:
        return Decision(DENY, CHECK_CARD,
                        f"{peer_id} ({peer.organisation}) is not on the partner allow-list")
    if peer.internal:
        return None
    if card is None or not card.resolved:
        return Decision(DENY, CHECK_CARD,
                        f"{peer_id} did not serve a resolvable agent card")
    if peer.display_name and card.name and card.name != peer.display_name:
        # The allow-list names an agent, not a URL. If something else is
        # answering at that address, the allow-list entry does not cover it.
        return Decision(
            DENY, CHECK_CARD,
            f"the agent card at {card.url} identifies as {card.name!r}, "
            f"but {peer_id} is registered as {peer.display_name!r}",
        )
    return None


def check_grant(reg: Registry, principal: Principal, peer_id: str) -> Decision | None:
    # Both must agree: the registry says who may be delegated to, and the
    # identity token the caller presented carries that as a scope. A token
    # issued before the grant existed therefore does not pick it up.
    if not reg.may_delegate_to(principal.agent_id, peer_id):
        return Decision(DENY, CHECK_GRANT,
                        f"{principal.agent_id} is not registered to delegate to {peer_id}")
    if peer_id not in principal.peer_scopes:
        return Decision(DENY, CHECK_GRANT,
                        f"the identity {principal.agent_id} presented carries no "
                        f"peer:{peer_id} scope")
    return None


def check_depth(reg: Registry, principal: Principal, current_depth: int) -> Decision | None:
    record = reg.agent(principal.agent_id)
    if record is None:
        return Decision(DENY, CHECK_DEPTH,
                        f"{principal.agent_id} is not registered, so it has no delegation budget")
    if current_depth + 1 > record.max_delegation_depth:
        return Decision(
            DENY, CHECK_DEPTH,
            f"delegation depth {current_depth + 1} exceeds the limit of "
            f"{record.max_delegation_depth} for {principal.agent_id}",
        )
    return None


def check_egress(reg: Registry, peer_id: str, classification: str) -> Decision | None:
    peer = reg.peer(peer_id)
    if peer is None:  # pragma: no cover - check_card refuses first
        return Decision(DENY, CHECK_EGRESS, f"{peer_id} is unknown")
    if classification not in peer.accepts_classifications:
        accepts = ", ".join(sorted(peer.accepts_classifications)) or "nothing"
        return Decision(
            DENY, CHECK_EGRESS,
            f"{classification} data may not leave for {peer_id}, which is "
            f"permitted to receive {accepts}",
        )
    return None


def evaluate(
    reg: Registry,
    principal: Principal,
    peer_id: str,
    *,
    classification: str,
    current_depth: int,
    card: AgentCardInfo | None,
) -> Decision:
    """All four checks, in order. First refusal wins."""
    for refusal in (
        check_card(reg, peer_id, card),
        check_grant(reg, principal, peer_id),
        check_depth(reg, principal, current_depth),
        check_egress(reg, peer_id, classification),
    ):
        if refusal is not None:
            return refusal
    return ALLOWED


# --------------------------------------------------------------------------
# The gateway
# --------------------------------------------------------------------------

class A2AGateway:
    """The only way out. Agents never hold a peer URL themselves."""

    def __init__(
        self,
        endpoints: dict[str, str] | None = None,
        reg: Registry | None = None,
        identity: IdentityProvider | None = None,
    ) -> None:
        self._registry = reg or registry()
        self._endpoints = dict(endpoints or {})
        self._identity = identity
        # Internal peers are in-process agents rather than remote services.
        self._internal: dict[str, Callable[[str], Awaitable[Any]]] = {}

    def register_endpoint(self, peer_id: str, url: str) -> None:
        self._endpoints[peer_id] = url

    def register_internal(self, peer_id: str, handler: Callable[[str], Awaitable[Any]]) -> None:
        self._internal[peer_id] = handler

    def endpoint_for(self, peer_id: str) -> str | None:
        peer = self._registry.peer(peer_id)
        return self._endpoints.get(peer_id) or (peer.url if peer else None)

    async def fetch_card(self, peer_id: str) -> AgentCardInfo | None:
        """Resolve the peer's published agent card.

        Deliberately done before the allow-list is consulted rather than after:
        the check is "is the agent at this address the one we approved", which
        needs the card the address is actually serving.
        """
        peer = self._registry.peer(peer_id)
        if peer is not None and peer.internal:
            return AgentCardInfo(name=peer.display_name, url="in-process",
                                 organisation=peer.organisation)
        url = self.endpoint_for(peer_id)
        if not url:
            return None
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{url.rstrip('/')}/.well-known/agent-card.json"
                )
                response.raise_for_status()
                card = response.json()
        except Exception:
            return AgentCardInfo.unresolvable(url)
        provider = card.get("provider") or {}
        return AgentCardInfo(
            name=card.get("name", ""),
            url=url,
            organisation=provider.get("organization", ""),
        )

    def outbound_headers(self, principal: Principal, depth: int,
                         chain: tuple[str, ...]) -> dict[str, str]:
        headers = {
            CALLER_HEADER: principal.agent_id,
            DEPTH_HEADER: str(depth + 1),
            CHAIN_HEADER: " -> ".join((*chain, principal.agent_id)),
        }
        # W3C trace context, so the hop and everything the peer does under it
        # land in the same trace as the request that caused them.
        telemetry.inject_context(headers)
        return headers

    async def delegate(
        self,
        principal: Principal,
        peer_id: str,
        task: str,
        *,
        classification: str = "internal",
    ) -> Delegation:
        depth = delegation_depth()
        chain = delegation_chain()
        card = await self.fetch_card(peer_id)
        decision = evaluate(
            self._registry, principal, peer_id,
            classification=classification, current_depth=depth, card=card,
        )

        result = await apply_decision(
            principal=principal,
            kind=A2A,
            target=peer_id,
            decision=decision,
            action="delegate",
            classification=classification,
            arguments={"task": task, "peer_url": self.endpoint_for(peer_id)},
        )
        if not result.allowed:
            return Delegation(result=result)

        reply = await self._invoke(principal, peer_id, task, depth, chain)
        return Delegation(result=result, reply=reply)

    async def _invoke(
        self,
        principal: Principal,
        peer_id: str,
        task: str,
        depth: int,
        chain: tuple[str, ...],
    ) -> str:
        handler = self._internal.get(peer_id)
        if handler is not None:
            # An internal hop. The callee runs one level deeper, with the chain
            # carried forward, so its own delegations are budgeted correctly.
            peer_principal = (
                self._identity.issue(peer_id) if self._identity is not None else principal
            )
            with acting_as(peer_principal, depth=depth + 1,
                           chain=(*chain, principal.agent_id)):
                response = await handler(task)
            return _as_text(response)

        url = self.endpoint_for(peer_id)
        if not url:  # pragma: no cover - check_card refuses first
            raise RuntimeError(f"no endpoint configured for {peer_id}")

        from agent_framework.a2a import A2AAgent

        headers = self.outbound_headers(principal, depth, chain)
        async with httpx.AsyncClient(timeout=30, headers=headers) as http_client:
            agent = A2AAgent(name=peer_id, url=url, http_client=http_client)
            response = await agent.run(task)
        return _as_text(response)


def _as_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text.strip()
    return str(response).strip()
