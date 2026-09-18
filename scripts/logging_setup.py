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
    "opentelemetry.sdk",
    "opentelemetry.exporter",
    "opentelemetry.exporter.otlp",
    "opentelemetry.exporter.otlp.proto.grpc",
    "opentelemetry.exporter.otlp.proto.grpc.exporter",
    "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
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

    # A collector that is not running must cost a retry, not a wall of red on
    # the projector. The spans still record and still carry trace ids.
    for name in (
        "opentelemetry.exporter.otlp.proto.grpc.exporter",
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def quiet_asyncio(verbose: bool | None = None) -> None:
    """Stop benign socket teardown from printing a traceback mid-act.

    Windows' proactor loop reports `ConnectionResetError` when the far end of an
    already-finished HTTP connection goes away. It is noise, it is unavoidable
    with several short-lived loopback servers per act, and a traceback on a
    projector reads as a failure.
    """
    import asyncio

    if verbose is None:
        verbose = os.getenv("DEMO_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on")
    if verbose:
        return

    loop = asyncio.get_running_loop()
    default = loop.get_exception_handler()

    def handler(loop_, context) -> None:
        exception = context.get("exception")
        if isinstance(exception, (ConnectionResetError, ConnectionAbortedError)):
            return
        if default is not None:
            default(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)
