"""The enforcement seam.

Everything that governs an agent action funnels through `apply_decision`. The
Agent Framework function middleware calls it. The Agent Hooks interceptor calls
it. The A2A gateway calls it. The MOCK executor calls it. None of them
reimplements any part of it.

That is what makes the claim in the talk structurally true rather than
aspirational: swapping `GOVERNANCE_ENGINE` or `DEMO_MODE` changes which code
*calls* this function, never what this function does.

One pass through it:

    open a `governance.policy` span
      -> evaluate (already done by the caller)
      -> write the audit row, before the caller is told anything
      -> if `approve`: park on the approval queue and wait
      -> publish to the console feed
      -> return a final allow/deny

The audit row is written before the answer is returned on purpose. A crash
between deciding and acting must leave the decision on record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from governance import settings
from governance.approvals import Resolution, approval_queue
from governance.audit import AuditEntry, audit_store, dump_arguments
from governance.context import delegation_chain, delegation_depth
from governance.events import bus
from governance.identity import Principal
from governance.policy import ALLOW, APPROVE, DENY, Decision, policy
from governance.registry import policy_input_for, registry
from governance import telemetry

TOOL = "tool"
A2A = "a2a"


class Denied(RuntimeError):
    """A governed action was refused. Carries the decision that refused it."""

    def __init__(self, result: GuardResult) -> None:
        super().__init__(result.message)
        self.result = result


@dataclass(frozen=True)
class GuardResult:
    """What the governance layer decided, and what actually happened."""

    principal_id: str
    kind: str
    target: str
    decision: Decision
    outcome: str  # "allow" or "deny" - final, after any approval
    audit_id: int | None = None
    trace_id: str | None = None
    resolution: Resolution | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def rule_id(self) -> str:
        return self.decision.rule_id

    @property
    def reason(self) -> str:
        return self.resolution.reason if self.resolution else self.decision.reason

    @property
    def display_outcome(self) -> str:
        """What the console and the narration show.

        Distinguishes an approval that was granted from one that timed out, which
        a plain allow/deny would hide.
        """
        if self.resolution is not None:
            return self.resolution.outcome
        return self.outcome

    @property
    def message(self) -> str:
        """What the agent - and therefore the model - is told."""
        if self.allowed:
            return f"{self.target} permitted ({self.rule_id})."
        return (
            f"Denied by Helix governance: {self.target} refused for "
            f"{self.principal_id}. Rule {self.rule_id}. {self.reason}"
        )


async def apply_decision(
    *,
    principal: Principal,
    kind: str,
    target: str,
    decision: Decision,
    action: str | None = None,
    classification: str | None = None,
    arguments: dict[str, Any] | None = None,
    engine: str | None = None,
) -> GuardResult:
    """Trace, audit, escalate and finalise one governance decision."""
    engine = engine or settings.governance_engine()
    mode = settings.demo_mode()
    chain = delegation_chain()
    depth = delegation_depth()

    with telemetry.policy_span(
        agent=principal.agent_id,
        entra_agent_id=principal.identity_label,
        target=target,
        kind=kind,
        classification=classification,
        engine=engine,
        mode=mode,
        delegation_depth=depth,
        delegation_chain=chain,
    ) as span:
        telemetry.record_decision(span, decision.decision, decision.rule_id, decision.reason)
        trace_id, span_id = telemetry.span_ids(span)

        entry = audit_store().record(
            AuditEntry(
                agent=principal.agent_id,
                entra_agent_id=principal.identity_label,
                kind=kind,
                target=target,
                action=action,
                classification=classification,
                decision=decision.decision,
                rule_id=decision.rule_id,
                reason=decision.reason,
                engine=engine,
                mode=mode,
                trace_id=trace_id,
                span_id=span_id,
                delegation_chain=" -> ".join(chain) or None,
                arguments=dump_arguments(arguments),
            )
        )

        resolution: Resolution | None = None
        outcome = ALLOW if decision.decision == ALLOW else DENY

        if decision.decision == APPROVE:
            _, resolution = await approval_queue().request(
                agent=principal.agent_id,
                kind=kind,
                target=target,
                arguments=arguments,
                classification=classification,
                rule_id=decision.rule_id,
                reason=decision.reason,
                audit_id=entry.id,
                delegation_chain=chain,
            )
            outcome = ALLOW if resolution.approved else DENY
            audit_store().update_resolution(
                entry.id or 0, resolution.outcome, resolution.resolved_by, resolution.reason
            )
            span.set_attribute(telemetry.ATTR_DECISION, resolution.outcome)
            span.add_event(
                "governance.approval",
                {
                    "governance.outcome": resolution.outcome,
                    "governance.resolved_by": resolution.resolved_by,
                },
            )

        result = GuardResult(
            principal_id=principal.agent_id,
            kind=kind,
            target=target,
            decision=decision,
            outcome=outcome,
            audit_id=entry.id,
            trace_id=trace_id,
            resolution=resolution,
        )

    bus().publish(
        "decision",
        agent=result.principal_id,
        entra_agent_id=principal.identity_label,
        kind=kind,
        target=target,
        outcome=result.display_outcome,
        rule_id=result.rule_id,
        reason=result.reason,
        classification=classification,
        trace_id=result.trace_id,
        audit_id=result.audit_id,
        engine=engine,
    )
    return result


async def guard_tool(
    principal: Principal,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    engine: str | None = None,
) -> GuardResult:
    """Evaluate and enforce one tool call."""
    # A Foundry toolbox namespaces the tools it re-publishes
    # (`clinical_tools___search_docs`). Policy is written against the tool, not
    # the route to it, so the namespace is stripped before evaluation - and the
    # audit row records the bare name too, so MOCK and LIVE rows are comparable.
    from governance.toolbox_live import strip_namespace

    tool = strip_namespace(tool)
    reg = registry()
    request = policy_input_for(reg, principal.agent_id, tool)
    decision = policy().evaluate(request)
    return await apply_decision(
        principal=principal,
        kind=TOOL,
        target=tool,
        decision=decision,
        action=request.action,
        classification=request.classification,
        arguments=arguments,
        engine=engine,
    )
