"""What an act observed.

Every act returns one of these. `uv run demo rehearse` asserts against it, so a
rehearsal fails loudly if an act stops producing the decision the talk depends
on - which is the whole reason the rehearsal exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Observation:
    """One thing an agent tried, and what the governance layer did about it."""

    agent: str
    target: str
    outcome: str
    rule_id: str = ""
    reason: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.agent, self.target)


@dataclass
class ActResult:
    act: int
    name: str
    observations: list[Observation] = field(default_factory=list)
    trace_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def record(self, agent: str, target: str, outcome: str,
               rule_id: str = "", reason: str = "") -> Observation:
        obs = Observation(agent, target, outcome, rule_id, reason)
        self.observations.append(obs)
        return obs

    def outcome(self, agent: str, target: str) -> str | None:
        for obs in self.observations:
            if obs.key == (agent, target):
                return obs.outcome
        return None

    def outcomes(self) -> dict[tuple[str, str], str]:
        return {obs.key: obs.outcome for obs in self.observations}
