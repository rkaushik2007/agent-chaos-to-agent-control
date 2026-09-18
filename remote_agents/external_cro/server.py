"""A partner agent, reachable over A2A.

Used twice, with two different identities:

* `external_cro`      - the contract research organisation. Allow-listed.
* `unknown_vendor`    - some other vendor's agent. Not allow-listed.

They run the same code on purpose. Being reachable, well-behaved and able to
answer is not the same as being permitted to be called, and the only thing that
distinguishes the two on stage is the governance decision. If the demo faked the
unknown vendor by making it unreachable, it would be proving that the network
blocks it - which is not the claim.

This is a real A2A service built on the official `a2a-sdk` server: it publishes
an agent card at `/.well-known/agent-card.json` and answers JSON-RPC, so
Agent Framework's `A2AAgent` talks to it over the real protocol. The answers
themselves are canned, because a partner agent improvising on stage helps nobody.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from a2a import helpers
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette

from hosting import serve_asgi

CANNED_ANSWERS = {
    "visit window": (
        "Week 24 visit window for HTX-204 is day 168 +/- 7 days. "
        "Confirmed against our copy of protocol version 3.0."
    ),
    "enrolment": "Enrolment at our managed sites is 84% of target as of this week.",
    "monitoring": "Next risk-based monitoring visit is scheduled for week 12.",
}

DEFAULT_ANSWER = (
    "Acknowledged. The study team will respond through the usual channel."
)

# Deliberately no PHI in any answer, and the partner says so, because act 3
# blocks a PHI payload on the way *out* - before this service ever sees it.
DECLINE_PHI = (
    "This endpoint is not contracted to receive participant-level data. "
    "Please route case-level questions through the safety mailbox."
)


@dataclass(frozen=True)
class PeerProfile:
    peer_id: str
    display_name: str
    organisation: str
    description: str


CRO = PeerProfile(
    peer_id="external_cro",
    display_name="Northwind CRO Study Agent",
    organisation="Northwind Clinical Research",
    description="Answers operational questions about sites Northwind manages for HTX-204.",
)

UNKNOWN_VENDOR = PeerProfile(
    peer_id="unknown_vendor",
    display_name="Vendor Assistant",
    organisation="unregistered",
    description="An agent that answers questions. Nobody at Helix approved it.",
)


def answer_for(question: str) -> str:
    lowered = question.lower()
    if "ae-" in lowered or "adverse event" in lowered or "case record" in lowered:
        return DECLINE_PHI
    for needle, reply in CANNED_ANSWERS.items():
        if needle in lowered:
            return reply
    return DEFAULT_ANSWER


def build_card(profile: PeerProfile, base_url: str) -> AgentCard:
    return AgentCard(
        name=profile.display_name,
        description=profile.description,
        version="1.0.0",
        provider={"organization": profile.organisation, "url": base_url},
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[AgentInterface(url=base_url, protocol_binding="JSONRPC")],
        skills=[
            AgentSkill(
                id="study-operations",
                name="Study operations Q&A",
                description="Answers operational questions about the HTX-204 study.",
                tags=["clinical", "operations"],
            )
        ],
    )


class CannedExecutor(AgentExecutor):
    """Answers from a fixed table. Records what it was asked, for the tests."""

    def __init__(self, profile: PeerProfile) -> None:
        self.profile = profile
        self.received: list[str] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        question = helpers.get_message_text(context.message) if context.message else ""
        self.received.append(question)

        # The task has to be on the queue before any status update referring to
        # it, otherwise the client rejects the response with
        # "Agent should enqueue Task before TaskStatusUpdateEvent event".
        task = context.current_task
        if task is None:
            task = helpers.new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        reply = answer_for(question)
        # The answer goes on the task as an artifact as well as on the final
        # status message. A2A clients differ in which of the two they read, and
        # a partner reply that arrives nowhere is not a useful demo.
        await updater.add_artifact([helpers.new_text_part(reply)], name="answer")
        await updater.complete(
            updater.new_agent_message([helpers.new_text_part(reply)])
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def build_app(profile: PeerProfile, base_url: str) -> tuple[Starlette, CannedExecutor]:
    executor = CannedExecutor(profile)
    card = build_card(profile, base_url)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url="/"),
    ]
    return Starlette(routes=routes), executor


@contextlib.asynccontextmanager
async def remote_agent(profile: PeerProfile) -> AsyncIterator[tuple[str, CannedExecutor]]:
    """Run the partner agent, yielding its base URL and executor.

    The agent card must advertise the service's own address, which is not known
    until the port is bound, so the app is built by a factory that `serve_asgi`
    calls once it has the URL.
    """
    holder: list[CannedExecutor] = []

    def factory(base_url: str) -> Starlette:
        app, executor = build_app(profile, base_url)
        holder.append(executor)
        return app

    async with serve_asgi(app_factory=factory) as base_url:
        yield base_url, holder[0]
