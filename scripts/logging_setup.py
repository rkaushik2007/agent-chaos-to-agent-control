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
    # uvicorn logs this at ERROR every time a server with a graceful-shutdown
    # timeout stops with anything still connected - which, for servers that live
    # for one act, is most of them. It is expected, it is not a failure, and with
    # `log_config=None` it reaches stderr through logging's last-resort handler
    # no matter what level the logger is set to. A filter on the logger is
    # checked before any handler, so it stops there. Every other uvicorn error
    # still gets through.
    logging.getLogger("uvicorn.error").addFilter(
        lambda record: "timeout graceful shutdown exceeded" not in record.getMessage()
    )

    for name in (
        "opentelemetry.exporter.otlp.proto.grpc.exporter",
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def quiet_asyncio(verbose: bool | None = None) -> None:
    """Stop benign socket teardown from printing a traceback mid-act.

    Installs on the *calling* loop. Each server thread installs its own; see
    `hosting.serving`.
    """
    if verbose is None:
        verbose = os.getenv("DEMO_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on")
    if verbose:
        return

    from hosting.serving import install_quiet_exception_handler

    install_quiet_exception_handler()
