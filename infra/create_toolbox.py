"""Create (or add a version to) the Helix toolbox in Microsoft Foundry.

    uv run python infra/create_toolbox.py            # create version from config
    uv run python infra/create_toolbox.py --v1       # only the four v1 tools
    uv run python infra/create_toolbox.py --show     # list versions, change nothing

Toolbox is in **public preview**. Every call here is documented at
https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox and
needs `azure-ai-projects` 2.3.0 or later (this repo pins 2.6.1 - see the note in
pyproject.toml about why not 2.7.0).

One practical constraint worth knowing before you try this: **Foundry must be
able to reach your MCP server.** The `clinical-tools` server in this repo runs
on loopback, and a managed service in Azure cannot dial your laptop. Publish it
first - an Azure Dev Tunnel is the quickest way - and set
`CLINICAL_TOOLS_PUBLIC_URL`. `docs/LIVE_SETUP.md` has the commands.

Creating a version never changes which version agents get. Promotion is a
separate, deliberate act: `infra/promote_toolbox.py`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from governance import settings  # noqa: E402
from governance.toolbox_live import consumer_endpoint, version_endpoint  # noqa: E402


def _client():
    from azure.ai.projects import AIProjectClient

    from governance.credentials import credential

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        raise SystemExit("FOUNDRY_PROJECT_ENDPOINT is not set. See docs/LIVE_SETUP.md.")
    return AIProjectClient(endpoint=endpoint, credential=credential()), endpoint


def server_label(version: str) -> str:
    """The namespace Foundry will prefix onto this source's tools."""
    prefix = os.environ.get("TOOLBOX_SERVER_LABEL_PREFIX", "clinical_tools").strip()
    return f"{prefix}_{version}"


def build_tools(version: str):
    """Turn `config/toolbox.yaml` into Foundry toolbox tool definitions."""
    from azure.ai.projects.models import MCPToolboxTool

    catalogue = yaml.safe_load(settings.TOOLBOX_FILE.read_text(encoding="utf-8"))
    published = catalogue["versions"][version]["tools"]

    # A toolbox version holds tool *sources*; Foundry discovers the individual
    # tools from the server. So "v1 publishes four tools" means the server behind
    # v1 exposes four - run it with CLINICAL_TOOLS_VERSION=v1 and point
    # CLINICAL_TOOLS_PUBLIC_URL_V1 at it. A single CLINICAL_TOOLS_PUBLIC_URL is
    # fine when both versions front the same server; the promotion then changes
    # the version but not the tool list.
    server_url = (
        os.environ.get(f"CLINICAL_TOOLS_PUBLIC_URL_{version.upper()}", "").strip()
        or os.environ.get("CLINICAL_TOOLS_PUBLIC_URL", "").strip()
    )
    if not server_url:
        raise SystemExit(
            "CLINICAL_TOOLS_PUBLIC_URL is not set.\n\n"
            "Foundry has to reach the clinical-tools MCP server, and it cannot\n"
            "reach loopback on your machine. Publish it first - see the\n"
            "'Publishing clinical-tools' section of docs/LIVE_SETUP.md - then\n"
            "set CLINICAL_TOOLS_PUBLIC_URL to its public /mcp URL."
        )

    # One MCP tool entry per server. Foundry discovers the individual tools from
    # the server itself; the catalogue decides which *version* publishes it.
    # The label is per catalogue version, and that is not cosmetic.
    #
    # Foundry caches MCP tool discovery against the `server_label` within a
    # project - not against the server URL, and not per toolbox version.
    # Verified: a toolbox whose only version points at a server serving four
    # tools still published five, because that label had once seen five. A
    # brand-new label against the same server published four.
    #
    # So two versions that are meant to expose different tool sets need
    # different labels. Governance strips the namespace before evaluating
    # policy, so the label never reaches a rule.
    return [
        MCPToolboxTool(
            server_label=server_label(version),
            server_url=server_url,
            # The demo's approval gate is Helix policy, in this repo's own
            # middleware, not Foundry's per-tool prompt. Two approval prompts for
            # one action would muddle the story rather than strengthen it.
            require_approval="never",
        )
    ], published


def create(version: str) -> None:
    project_client, endpoint = _client()
    tools, published = build_tools(version)
    toolbox_name = os.environ.get("TOOLBOX_NAME", "helix-clinical-tools").strip()

    with project_client:
        created = project_client.toolboxes.create_version(
            name=toolbox_name,
            description=(
                "Helix Therapeutics clinical trial tools. "
                f"Catalogue version {version}: {', '.join(published)}."
            ),
            tools=tools,
        )
        print(f"created toolbox '{created.name}' version {created.version}")
        print(f"  catalogue version : {version} ({', '.join(published)})")
        print(f"  developer endpoint: {version_endpoint(endpoint, created.name, created.version)}")
        print(f"  consumer endpoint : {consumer_endpoint(endpoint, created.name)}")
        print()
        print(f"Record the mapping in config/toolbox.yaml so LIVE can promote it:")
        print(f"  versions.{version}.live_version: \"{created.version}\"")
        print()
        print("The consumer endpoint always serves default_version. The first")
        print("version of a new toolbox becomes the default automatically; any")
        print("later version does not until you promote it:")
        print(f"  uv run python infra/promote_toolbox.py {created.version}")


def show() -> None:
    project_client, endpoint = _client()
    toolbox_name = os.environ.get("TOOLBOX_NAME", "helix-clinical-tools").strip()
    with project_client:
        # NOTE: the Learn docs name these `list_toolbox_versions` /
        # `get_toolbox_version` / `delete_toolbox_version`. azure-ai-projects
        # 2.6.1 ships them as `list_versions` / `get_version` /
        # `delete_version`. Toolbox is preview; check `dir()` if this breaks.
        versions = list(project_client.toolboxes.list_versions(name=toolbox_name))
        if not versions:
            print(f"no versions for toolbox '{toolbox_name}'")
            return
        toolbox = project_client.toolboxes.get(name=toolbox_name)
        default = getattr(toolbox, "default_version", "?")
        for item in versions:
            version = getattr(item, "version", "?")
            marker = "  <- default" if str(version) == str(default) else ""
            print(f"  version {version}  {getattr(item, 'description', '')}{marker}")
        print()
        print(f"  consumer endpoint: {consumer_endpoint(endpoint, toolbox_name)}")


def main() -> int:
    settings.load_env()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--v1", action="store_true",
                        help="publish the four v1 tools instead of the v2 five")
    parser.add_argument("--show", action="store_true",
                        help="list existing versions and change nothing")
    args = parser.parse_args()

    if args.show:
        show()
        return 0
    create("v1" if args.v1 else "v2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
