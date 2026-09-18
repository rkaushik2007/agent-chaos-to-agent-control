"""Keep the stage output readable.

MCP, httpx, uvicorn and Agent Framework all log at INFO by default, which turns
every act into a wall of transport chatter on a projector. The acts silence them
and narrate deliberately instead. `DEMO_VERBOSE=1` puts the chatter back for
debugging.
"""

from __future__ import annotations

import logging
import os
import warnings

NOISY = (
    "mcp",
    "mcp.server",
    "mcp.server.streamable_http",
    "mcp.server.streamable_http_manager",
    "mcp.client",
    "httpx",
    "httpcore",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "asyncio",
    "agent_framework",
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "opentelemetry",
    "a2a",
)


def quiet(verbose: bool | None = None) -> None:
    if verbose is None:
        verbose = os.getenv("DEMO_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on")
    if verbose:
        logging.basicConfig(level=logging.INFO)
        return

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    for name in NOISY:
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        logger.propagate = False
    # agent-hooks-sdk announces its experimental status on first use. The README
    # and LIVE_SETUP both say it is experimental; saying it again mid-demo in red
    # is not informative.
    warnings.filterwarnings("ignore", category=UserWarning, module="agent_framework.*")
    warnings.filterwarnings("ignore", message=".*[Ee]xperimental.*")
