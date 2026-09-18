"""The policy evaluator. Pure, deny by default, unit-tested.

Nothing in here imports Agent Framework, touches the network, reads the clock or
writes a log. It takes a `PolicyInput` and returns a `Decision`. That is what
makes the matrix in `tests/test_policy.py` an actual proof rather than a
demonstration, and it is why the same evaluator can sit behind both enforcement
engines without either one changing its answers.

Evaluation order, first match wins:

    1. REGISTRY-000   the agent is not registered
    2. CATALOGUE-000  the tool is not classified
    3. CLEARANCE-000  the agent does not hold the data classification
    4. GRANT-000      the tool is not among the agent's registered grants
    5. rules          first matching rule in config/policy.yaml
    6. DEFAULT-DENY   nothing matched

Steps 1-4 are structural: they come from the registry, not from the rules. They
are the reason a careless `allow` rule cannot hand a Clinical Operations agent a
patient record.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from governance import settings

Effect = Literal["allow", "deny", "approve"]

ALLOW: Effect = "allow"
DENY: Effect = "deny"
APPROVE: Effect = "approve"


@dataclass(frozen=True)
class Decision:
    """The answer, plus enough of the reasoning to put it on a slide."""

    decision: Effect
    rule_id: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    @property
    def denied(self) -> bool:
        return self.decision == DENY

    @property
    def needs_approval(self) -> bool:
        return self.decision == APPROVE

    def __str__(self) -> str:
        return f"{self.decision.upper()} ({self.rule_id}) {self.reason}"


@dataclass(frozen=True)
class PolicyInput:
    """Everything the evaluator is allowed to know.

    Deliberately primitive: no registry object, no agent instance, no context.
    If a decision needs a new fact, the fact has to be named here, which makes
    the widening visible in review.
    """

    agent: str | None
    tool: str
    action: str | None
    classification: str | None
    registered: bool
    clearance: frozenset[str] = frozenset()
    grants: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Rule:
    id: str
    effect: Effect
    agents: frozenset[str]
    tools: frozenset[str]
    actions: frozenset[str]
    classifications: frozenset[str]
    description: str = ""

    def matches(self, req: PolicyInput) -> bool:
        if self.agents and req.agent not in self.agents:
            return False
        if self.tools and req.tool not in self.tools:
            return False
        if self.actions and req.action not in self.actions:
            return False
        if self.classifications and req.classification not in self.classifications:
            return False
        return True


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...]
    default_effect: Effect = DENY

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        path = path or settings.POLICY_FILE
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing")
        return cls._from_doc(
            yaml.safe_load(path.read_text(encoding="utf-8")) or {}, source=path.name
        )

    @classmethod
    def load_from_text(cls, text: str, *, source: str = "<inline policy>") -> Policy:
        """Load a policy from a YAML string. Used by the tests to assert on
        policy shapes that must never be allowed to ship."""
        return cls._from_doc(yaml.safe_load(textwrap.dedent(text)) or {}, source=source)

    @classmethod
    def _from_doc(cls, doc: dict, *, source: str) -> Policy:
        default_effect = doc.get("default_effect", DENY)
        if default_effect not in (ALLOW, DENY, APPROVE):
            raise ValueError(f"default_effect must be allow/deny/approve, got {default_effect!r}")
        if default_effect != DENY:
            # Loud on purpose. A policy file whose default is not deny is a
            # policy file that will one day permit something nobody intended.
            raise ValueError(
                f"{source}: default_effect is {default_effect!r}. This governance layer "
                "denies by default and does not support any other posture."
            )

        rules = []
        seen: set[str] = set()
        for raw in doc.get("rules") or []:
            rule_id = raw["id"]
            if rule_id in seen:
                raise ValueError(f"{source}: duplicate rule id {rule_id!r}")
            seen.add(rule_id)
            effect = raw["effect"]
            if effect not in (ALLOW, DENY, APPROVE):
                raise ValueError(f"{rule_id}: effect must be allow/deny/approve, got {effect!r}")
            when = raw.get("when") or {}
            rules.append(
                Rule(
                    id=rule_id,
                    effect=effect,
                    agents=frozenset(raw.get("agents") or ()),
                    tools=frozenset(raw.get("tools") or ()),
                    actions=frozenset(raw.get("actions") or ()),
                    classifications=frozenset(when.get("classification_in") or ()),
                    description=" ".join((raw.get("description") or "").split()),
                )
            )
        return cls(rules=tuple(rules), default_effect=default_effect)

    def evaluate(self, req: PolicyInput) -> Decision:
        if not req.registered or not req.agent:
            return Decision(
                DENY, "REGISTRY-000",
                f"{req.agent or 'unknown agent'} is not in the agent registry",
            )

        if not req.classification or not req.action:
            return Decision(
                DENY, "CATALOGUE-000",
                f"{req.tool} is not classified in the tool catalogue",
            )

        if req.classification not in req.clearance:
            held = ", ".join(sorted(req.clearance)) or "nothing"
            return Decision(
                DENY, "CLEARANCE-000",
                f"{req.agent} holds clearance for {held}, not {req.classification}",
            )

        if req.tool not in req.grants:
            return Decision(
                DENY, "GRANT-000",
                f"{req.tool} is not among the tools {req.agent} is registered to use",
            )

        for rule in self.rules:
            if rule.matches(req):
                return Decision(rule.effect, rule.id, rule.description or
                                f"matched {rule.id}")

        return Decision(
            self.default_effect, "DEFAULT-DENY",
            f"no rule permits {req.agent} to {req.action} {req.tool}",
        )


@lru_cache(maxsize=1)
def policy() -> Policy:
    """The process-wide policy. Call `reload()` after editing the file."""
    return Policy.load()


def reload() -> Policy:
    policy.cache_clear()
    return policy()
