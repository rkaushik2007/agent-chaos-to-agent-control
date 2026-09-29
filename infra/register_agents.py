"""Register the Helix agents in Microsoft Foundry, so the fleet is visible there.

    uv run python infra/register_agents.py            # register all three
    uv run python infra/register_agents.py --show     # list them, change nothing
    uv run python infra/register_agents.py --delete   # remove the registrations

Why this exists. Acts 2-4 run the agents on *this machine*, so by default
Foundry has a toolbox to show and nothing that looks like an agent. An
**external agent** registration closes that gap: a metadata record saying "an
agent called trial_ops exists, and it emits telemetry under this id". Foundry
then matches spans in the project's Application Insights by `gen_ai.agent.id`
and shows them under Agents -> trial_ops -> Traces. Nothing is hosted, proxied
or invoked by Foundry, and the agent keeps running where it runs.

    https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/register-external-agent

Cost: nothing. No gateway, no endpoint, no compute. The one prerequisite that
matters is Application Insights connected to the project, which LIVE already
needs for act 4.

Not this, deliberately: Foundry *control plane* custom agents
(Operate -> Register asset). Those give a block/unblock switch and per-call HTTP
traces, but they require an AI gateway (Azure API Management) in front, one
exclusive reachable endpoint per agent, and they hand out a new client URL that
callers must use instead of the original. That governs the *transport*. This
repo's argument is that governance belongs at the tool and delegation boundary,
which is where the middleware sits.

    https://learn.microsoft.com/en-us/azure/foundry/control-plane/register-custom-agent

PREVIEW: external agents are public preview. Create and update requests need the
`Foundry-Features: ExternalAgents=V1Preview` header, which the Python SDK sends
when the client is built with `allow_preview=True`. Needs azure-ai-projects
2.3.0 or later; this repo pins 2.6.1.

`shadow_agent` is not registered, and that is the point. The registry is the only
source of an agent's rights, and Foundry's agent list should say the same thing:
an agent nobody owns is an agent nobody can see.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from governance import settings  # noqa: E402
from governance.registry import AgentRecord, registry  # noqa: E402


def _client():
    from azure.ai.projects import AIProjectClient

    from governance.credentials import credential

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        raise SystemExit("FOUNDRY_PROJECT_ENDPOINT is not set. See docs/LIVE_SETUP.md.")
    # allow_preview is what sends the ExternalAgents=V1Preview feature header.
    # Without it the service rejects the create outright.
    return AIProjectClient(
        endpoint=endpoint, credential=credential(), allow_preview=True
    )


def otel_agent_id(record: AgentRecord) -> str:
    """The `gen_ai.agent.id` the running agent actually emits.

    `agents.runner` builds every agent as `Agent(id=principal.agent_id)`, and
    Agent Framework puts that on its `invoke_agent` spans; `telemetry.policy_span`
    repeats it on the governance span. So the id is the registry key, derived
    here rather than typed out so the two cannot drift.
    """
    return record.id


def foundry_agent_name(record: AgentRecord) -> str:
    """The name Foundry will accept, which is not the registry key.

    The docs say an agent name may contain "alphanumeric characters, hyphens, and
    underscores". The service disagrees: `safety_triage` is rejected with
    "Must start and end with alphanumeric characters, can contain hyphens in the
    middle". Verified against the live API. So underscores become hyphens.

    This is exactly the case the docs say to set `otel_agent_id` explicitly for -
    the running agent emits `safety_triage`, the registration is called
    `safety-triage`, and without the explicit id the default would be the name
    and would match nothing.
    """
    return record.id.replace("_", "-")


def description_of(record: AgentRecord) -> str:
    return (
        f"{record.display_name} - {record.business_unit}. {record.purpose} "
        "Runs outside Foundry, governed by the Helix governance layer."
    )


def metadata_for(record: AgentRecord) -> dict[str, str]:
    """What the registry knows, carried into Foundry.

    Worth doing because it lets the agent list answer the first two questions an
    auditor asks - who owns this, and what is it cleared for - without opening
    this repo.
    """
    return {
        "owner": record.owner,
        "business_unit": record.business_unit,
        "data_clearance": ",".join(sorted(record.data_clearance)),
        "granted_tools": ",".join(sorted(record.allowed_tools)),
        "governed_by": "helix-governance-layer",
    }


def identity_env_var(record: AgentRecord) -> str:
    """The variable `config/agents.yaml` expands into `entra_agent_id`.

    Must match the `${...}` placeholders in that file.
    """
    return f"{record.id.upper()}_AGENT_IDENTITY_ID"


def _latest(details):
    return getattr(getattr(details, "versions", None), "latest", None)


def _registered_otel_id(details) -> str | None:
    """The otel id on the latest revision, or None if it cannot be read.

    Defensive because the shape is preview. The documented path is
    `agent.versions.latest.definition.otel_agent_id`.
    """
    return getattr(getattr(_latest(details), "definition", None), "otel_agent_id", None)


def _entra_identity(details) -> tuple[str | None, str | None]:
    """The Entra agent identity Foundry provisions for a registration.

    Not documented on the external-agent page, and genuinely useful: registering
    the agent makes Foundry mint a `ManagedAgentIdentityBlueprint` and an
    instance identity for it, so the agent has a real Entra principal even though
    Foundry never runs it. That principal id is what this repo stamps on every
    span and audit row as `entra.agent_id`.

    It does *not* mean a locally-run agent can acquire a token **as** that
    identity - that exchange is still Agent Service's to perform, and
    `governance/identity_live.py` says so. What it does mean is that the id we
    attribute decisions to is now a real directory object rather than a label.
    """
    latest = _latest(details)
    instance = getattr(latest, "instance_identity", None)
    blueprint = getattr(latest, "blueprint_reference", None)
    return (
        getattr(instance, "principal_id", None),
        getattr(blueprint, "blueprint_id", None),
    )


def register() -> int:
    from azure.ai.projects.models import ExternalAgentDefinition
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    reg = registry()
    with _client() as project:
        for agent_id in sorted(reg.agents):
            record = reg.require_agent(agent_id)
            wanted = otel_agent_id(record)
            name = foundry_agent_name(record)

            try:
                existing = project.agents.get(agent_name=name)
            except ResourceNotFoundError:
                existing = None
            if existing is not None and _registered_otel_id(existing) == wanted:
                print(f"  {name:<14} already registered, gen_ai.agent.id={wanted}")
                continue

            try:
                project.agents.create_version(
                    agent_name=name,
                    description=description_of(record),
                    definition=ExternalAgentDefinition(otel_agent_id=wanted),
                    metadata=metadata_for(record),
                )
            except HttpResponseError as exc:
                print(f"  {name:<14} FAILED: {str(exc.message).splitlines()[0]}")
                return 1
            verb = "updated" if existing is not None else "registered"
            print(f"  {name:<14} {verb}, gen_ai.agent.id={wanted}")

    print()
    print("Foundry portal -> your project -> Agents. Pick one, then the Traces tab.")
    print("Traces show up 2-5 minutes after a LIVE run. Spans already in")
    print("Application Insights are matched too, so an earlier run counts.")
    return 0


def _external_agents(project) -> list:
    from azure.ai.projects.models import AgentKind

    return sorted(project.agents.list(kind=AgentKind.EXTERNAL), key=lambda d: d.name)


def show() -> int:
    with _client() as project:
        found = _external_agents(project)
        if not found:
            print("no external agents registered in this project")
            return 0
        for details in found:
            principal, blueprint = _entra_identity(details)
            print(f"  {details.name:<14} gen_ai.agent.id={_registered_otel_id(details)}")
            print(f"  {'':<14} entra agent identity {principal or 'none'}")
            if blueprint:
                print(f"  {'':<14} blueprint            {blueprint}")
    return 0


def env() -> int:
    """Print the `.env` lines that stamp the real Entra ids on every decision.

    Kept separate from `--show` so the output can be appended to `.env` as is.
    Registering an agent is what creates these identities, so this only works
    after `register`.
    """
    reg = registry()
    by_name = {foundry_agent_name(record): record for record in reg.agents.values()}
    with _client() as project:
        lines = []
        for details in _external_agents(project):
            record = by_name.get(details.name)
            principal, _ = _entra_identity(details)
            if record is None or not principal:
                continue
            lines.append(f"{identity_env_var(record)}={principal}")
    if not lines:
        print("# nothing to set - register the agents first")
        return 1
    print("# Entra agent identities provisioned by Foundry. Append to .env")
    print("# (which is git-ignored) so LIVE stamps a real directory object id")
    print("# instead of 'unconfigured:<agent>' on every span and audit row.")
    for line in lines:
        print(line)
    return 0


def delete() -> int:
    from azure.core.exceptions import ResourceNotFoundError

    reg = registry()
    with _client() as project:
        for agent_id in sorted(reg.agents):
            name = foundry_agent_name(reg.require_agent(agent_id))
            try:
                # force removes every internal revision in one go. Deleting a
                # registration does not touch the running agent.
                project.agents.delete(agent_name=name, force=True)
            except ResourceNotFoundError:
                print(f"  {name:<14} was not registered")
                continue
            print(f"  {name:<14} registration deleted")
    return 0


def main() -> int:
    settings.load_env()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true",
                       help="list the external agents already registered")
    group.add_argument("--env", action="store_true",
                       help="print the .env lines for the Entra agent identities")
    group.add_argument("--delete", action="store_true",
                       help="remove the registrations (the agents keep running)")
    args = parser.parse_args()

    if args.show:
        return show()
    if args.env:
        return env()
    if args.delete:
        return delete()
    return register()


if __name__ == "__main__":
    raise SystemExit(main())
