"""One place to build the Azure credential, with a timeout that survives a stage.

`AzureCliCredential` shells out to `az account get-access-token` and allows it
**10 seconds** by default. A cached token returns instantly, so this is invisible
until the cached token expires - then the call makes a round trip to Entra, and on
a conference network that can take longer than ten seconds. It fails with

    CredentialUnavailableError: Failed to invoke the Azure CLI

which reads like a broken installation rather than a slow network, and sent a
verification run of this repo chasing the wrong problem. `az` on the command line
worked fine the whole time.

So the timeout is raised here, once, and every LIVE caller comes through this
module. MOCK never calls it and does not need `azure-identity` installed, which is
why the imports are inside the functions.

`AZURE_USE_CLI_CREDENTIAL=false` switches to `DefaultAzureCredential` for a
deployment with a managed identity. The default is the CLI because that is the
demo case, and because `DefaultAzureCredential` probes every source in turn while
an audience watches.
"""

from __future__ import annotations

import os

# Generous rather than tight: this bounds a subprocess that normally returns in
# milliseconds, so the only thing a larger number costs is a longer wait in the
# case that is already broken.
DEFAULT_CLI_PROCESS_TIMEOUT = 45


def cli_process_timeout() -> int:
    raw = os.getenv("AZURE_CLI_PROCESS_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_CLI_PROCESS_TIMEOUT
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_CLI_PROCESS_TIMEOUT


def use_cli_credential() -> bool:
    return os.getenv("AZURE_USE_CLI_CREDENTIAL", "true").strip().lower() in (
        "true", "1", "yes", "on",
    )


def credential():
    """A synchronous Azure credential."""
    from azure.identity import AzureCliCredential, DefaultAzureCredential

    if use_cli_credential():
        return AzureCliCredential(process_timeout=cli_process_timeout())
    return DefaultAzureCredential(process_timeout=cli_process_timeout())


def async_credential():
    """The same choice, for the async clients (the Foundry chat client)."""
    from azure.identity.aio import AzureCliCredential, DefaultAzureCredential

    if use_cli_credential():
        return AzureCliCredential(process_timeout=cli_process_timeout())
    return DefaultAzureCredential(process_timeout=cli_process_timeout())
