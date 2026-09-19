"""The agent registry.

Three things are loaded here, and together they are the structural half of
governance:

* `config/agents.yaml`  - who the agents are, who owns them, what they hold
  clearance for, what they may be granted, and who they may delegate to.
* `config/toolbox.yaml` - what each tool does and what class of data it touches.
* `config/policy.yaml`  - loaded separately by `governance.policy`.

An agent that is not in the registry has no record, therefore no clearance and
no grants, therefore is denied everything. That is not a special case in the
policy - it falls out of the registry being the only source of an agent's
rights.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from governance import settings
from governance.policy import PolicyInput


@dataclass(frozen=True)
class AgentRecord:
    id: str
    display_name: str
    owner: str
    business_unit: str
    purpose: str
    data_clearance: frozenset[str]
    allowed_tools: frozenset[str]
    allowed_peers: tuple[str, ...]
    max_delegation_depth: int
    # LIVE only. None in MOCK, and None in LIVE until it is configured.
    entra_agent_id: str | None = None

    @property
    def identity_label(self) -> str:
        return self.entra_agent_id or f"mock:{self.id}"


@dataclass(frozen=True)
class PeerRecord:
    id: str
    display_name: str
    owner: str
    organisation: str
    allow_listed: bool
    accepts_classifications: frozenset[str]
    url: str | None = None
    # True when the peer is also a registered agent in this deployment, i.e. an
    # internal hop rather than a call across an organisational boundary.
    internal: bool = False


@dataclass(frozen=True)
class ToolRecord:
    name: str
    action: str
    classification: str
    description: str = ""


@dataclass(frozen=True)
class Registry:
    agents: dict[str, AgentRecord]
    peers: dict[str, PeerRecord]
    tools: dict[str, ToolRecord]
    toolbox_versions: dict[str, tuple[str, ...]]
    # catalogue version name -> the version id Foundry assigned (LIVE only)
    live_versions: dict[str, str]
    default_version: str
    server_name: str = "clinical-tools"
    _source: tuple[Path, ...] = field(default=(), compare=False)

    # -- agents ------------------------------------------------------------

    def agent(self, agent_id: str) -> AgentRecord | None:
        return self.agents.get(agent_id)

    def is_registered(self, agent_id: str) -> bool:
        return agent_id in self.agents

    def require_agent(self, agent_id: str) -> AgentRecord:
        record = self.agents.get(agent_id)
        if record is None:
            raise KeyError(f"{agent_id!r} is not a registered agent")
        return record

    # -- tools -------------------------------------------------------------

    def tool(self, name: str) -> ToolRecord | None:
        return self.tools.get(name)

    def classification_of(self, tool_name: str) -> str | None:
        record = self.tools.get(tool_name)
        return record.classification if record else None

    def action_of(self, tool_name: str) -> str | None:
        record = self.tools.get(tool_name)
        return record.action if record else None

    def live_version_of(self, version: str) -> str:
        """The Foundry version id for a catalogue version name.

        Falls back to the name itself so a deployment that numbers its versions
        the same way needs no mapping at all.
        """
        return self.live_versions.get(version, version)

    def tools_in_version(self, version: str) -> tuple[str, ...]:
        try:
            return self.toolbox_versions[version]
        except KeyError:
            raise KeyError(
                f"Unknown toolbox version {version!r}. "
                f"Known: {', '.join(sorted(self.toolbox_versions))}"
            ) from None

    def tools_for(self, agent_id: str, version: str | None = None) -> tuple[str, ...]:
        """The tool list this agent should be able to see.

        This is the toolbox's filtered `tools/list`: the intersection of what
        the active toolbox version publishes and what the agent is registered to
        be granted. An unregistered agent sees nothing at all.
        """
        record = self.agents.get(agent_id)
        if record is None:
            return ()
        published = self.tools_in_version(version or self.default_version)
        return tuple(name for name in published if name in record.allowed_tools)

    # -- peers -------------------------------------------------------------

    def peer(self, peer_id: str) -> PeerRecord | None:
        return self.peers.get(peer_id)

    def may_delegate_to(self, agent_id: str, peer_id: str) -> bool:
        record = self.agents.get(agent_id)
        return bool(record and peer_id in record.allowed_peers)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing")
    return settings.expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def load_registry(
    agents_file: Path | None = None,
    toolbox_file: Path | None = None,
) -> Registry:
    agents_file = agents_file or settings.AGENTS_FILE
    toolbox_file = toolbox_file or settings.TOOLBOX_FILE

    agents_doc = _load_yaml(agents_file)
    toolbox_doc = _load_yaml(toolbox_file)

    agents: dict[str, AgentRecord] = {}
    for agent_id, spec in (agents_doc.get("agents") or {}).items():
        agents[agent_id] = AgentRecord(
            id=agent_id,
            display_name=spec.get("display_name", agent_id),
            owner=spec.get("owner", "unknown"),
            business_unit=spec.get("business_unit", "unknown"),
            purpose=spec.get("purpose", ""),
            data_clearance=frozenset(spec.get("data_clearance") or ()),
            allowed_tools=frozenset(spec.get("allowed_tools") or ()),
            allowed_peers=tuple(spec.get("allowed_peers") or ()),
            max_delegation_depth=int(spec.get("max_delegation_depth", 1)),
            entra_agent_id=spec.get("entra_agent_id") or None,
        )

    peers: dict[str, PeerRecord] = {}
    for peer_id, spec in (agents_doc.get("peers") or {}).items():
        peers[peer_id] = PeerRecord(
            id=peer_id,
            display_name=spec.get("display_name", peer_id),
            owner=spec.get("owner", "unknown"),
            organisation=spec.get("organisation", "unknown"),
            allow_listed=bool(spec.get("allow_listed", False)),
            accepts_classifications=frozenset(spec.get("accepts_classifications") or ()),
            url=spec.get("url") or None,
        )

    # A registered agent is implicitly also a peer: delegating to a colleague is
    # still a delegation and still passes the gateway. It is marked internal so
    # the gateway can tell an in-house hop from one that leaves the building.
    for agent_id, record in agents.items():
        peers.setdefault(
            agent_id,
            PeerRecord(
                id=agent_id,
                display_name=record.display_name,
                owner=record.owner,
                organisation="Helix Therapeutics",
                allow_listed=True,
                accepts_classifications=record.data_clearance,
                url=None,
                internal=True,
            ),
        )

    tools = {
        name: ToolRecord(
            name=name,
            action=spec["action"],
            classification=spec["classification"],
            description=spec.get("description", ""),
        )
        for name, spec in (toolbox_doc.get("tools") or {}).items()
    }

    version_specs = toolbox_doc.get("versions") or {}
    versions = {
        version: tuple(spec.get("tools") or ())
        for version, spec in version_specs.items()
    }
    live_versions = {
        version: str(spec["live_version"])
        for version, spec in version_specs.items()
        if spec.get("live_version")
    }
    default_version = toolbox_doc.get("default_version") or next(iter(versions), "v1")

    unknown = {t for names in versions.values() for t in names} - set(tools)
    if unknown:
        raise ValueError(
            f"{toolbox_file.name} publishes uncatalogued tools: {', '.join(sorted(unknown))}. "
            "A tool with no classification cannot be governed."
        )

    return Registry(
        agents=agents,
        peers=peers,
        tools=tools,
        toolbox_versions=versions,
        live_versions=live_versions,
        default_version=default_version,
        server_name=(toolbox_doc.get("server") or {}).get("name", "clinical-tools"),
        _source=(agents_file, toolbox_file),
    )


def policy_input_for(reg: Registry, agent_id: str, tool_name: str) -> PolicyInput:
    """Collect the facts the evaluator is allowed to see.

    This is the only bridge between the registry and the policy. `policy.py`
    never imports the registry, so every fact a decision rests on has to pass
    through this function - and is therefore visible in one place.
    """
    record = reg.agent(agent_id)
    tool = reg.tool(tool_name)
    return PolicyInput(
        agent=agent_id,
        tool=tool_name,
        action=tool.action if tool else None,
        classification=tool.classification if tool else None,
        registered=record is not None,
        clearance=record.data_clearance if record else frozenset(),
        grants=record.allowed_tools if record else frozenset(),
    )


@lru_cache(maxsize=1)
def registry() -> Registry:
    """The process-wide registry. Call `reload()` after editing the config."""
    return load_registry()


def reload() -> Registry:
    registry.cache_clear()
    return registry()
