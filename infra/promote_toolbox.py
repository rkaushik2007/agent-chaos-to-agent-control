"""Promote a Foundry toolbox version to default.

    uv run python infra/promote_toolbox.py 2

This is act 2's live beat, for real: the consumer endpoint always serves
`default_version`, so promoting changes what every connected agent gets with no
redeploy, no restart and no agent code change.

    toolboxes.update(name=..., default_version=...)

https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox

`default_version` cannot be empty, and you must not delete the version that is
currently default - promote another one first.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from governance import settings  # noqa: E402
from governance.toolbox_live import consumer_endpoint  # noqa: E402


def promote(version: str) -> int:
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    toolbox_name = os.environ.get("TOOLBOX_NAME", "helix-clinical-tools").strip()
    if not endpoint:
        raise SystemExit("FOUNDRY_PROJECT_ENDPOINT is not set. See docs/LIVE_SETUP.md.")

    with AIProjectClient(endpoint=endpoint, credential=AzureCliCredential()) as client:
        before = getattr(client.toolboxes.get(name=toolbox_name), "default_version", "?") \
            if hasattr(client.toolboxes, "get") else "?"
        toolbox = client.toolboxes.update(name=toolbox_name, default_version=version)
        after = getattr(toolbox, "default_version", version)

    print(f"toolbox '{toolbox_name}': default_version {before} -> {after}")
    print(f"  consumer endpoint: {consumer_endpoint(endpoint, toolbox_name)}")
    print("  Every agent on that endpoint now gets this version. Nothing was redeployed.")
    return 0


def main() -> int:
    settings.load_env()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="the toolbox version id to make default")
    return promote(parser.parse_args().version)


if __name__ == "__main__":
    raise SystemExit(main())
