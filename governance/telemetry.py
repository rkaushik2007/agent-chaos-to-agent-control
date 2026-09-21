"""Telemetry.

Agent Framework already emits OpenTelemetry spans that follow the GenAI
semantic conventions - `invoke_agent`, `chat`, `execute_tool` - so this module
does not reinvent any of that. It does three things on top:

1. Turns the providers on, from environment variables, via
   `agent_framework.observability.configure_otel_providers()`.
2. Adds one span of its own, `governance.policy`, carrying the four attributes
   the session argues an enterprise actually needs on every agent action:
   `entra.agent_id`, `governance.decision`, `governance.rule_id` and
   `data.classification`.
3. Propagates trace context across the A2A hop, so a delegated denial and the
   request that caused it appear under one trace id.

Note on MCP: Agent Framework injects trace context into `tools/call` requests
for MCP sessions the agent process opens itself, which is what the local toolbox
is. It cannot do so for a hosted Foundry toolbox, because the service issues
that request, not us. See docs/LIVE_SETUP.md.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, format_span_id, format_trace_id

# Attribute names. Named once so the console, the audit store and the spans
# cannot drift apart.
ATTR_AGENT_ID = "entra.agent_id"
ATTR_AGENT = "helix.agent"
ATTR_DECISION = "governance.decision"
ATTR_RULE_ID = "governance.rule_id"
ATTR_CLASSIFICATION = "data.classification"
ATTR_REASON = "governance.reason"
ATTR_ENGINE = "governance.engine"
ATTR_MODE = "governance.mode"
ATTR_TARGET = "governance.target"
ATTR_KIND = "governance.target_kind"
ATTR_DELEGATION_DEPTH = "governance.delegation_depth"
ATTR_DELEGATION_CHAIN = "governance.delegation_chain"
ATTR_PEER = "governance.peer"

_TRACER_NAME = "helix.governance"
_configured = False


def instrumentation_enabled() -> bool:
    return os.getenv("ENABLE_INSTRUMENTATION", "true").strip().lower() not in (
        "false", "0", "no", "off",
    )


DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"

_EXPORTER_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "ENABLE_CONSOLE_EXPORTERS",
)


def configure(service_name: str | None = None) -> bool:
    """Turn telemetry on. Returns whether spans are actually being recorded.

    `configure_otel_providers()` installs providers only if at least one
    exporter is configured. With none, it quietly does nothing and every span is
    a `NonRecordingSpan` with no trace id - which would cost act 4 the one thing
    it exists to show, and would do it silently, on stage, because somebody
    forgot to copy `.env`.

    So if nothing is configured we point at the default OTLP endpoint anyway.
    Spans then record and carry real trace ids whether or not a collector is
    listening; an unreachable collector costs a background retry, not the act.
    """
    global _configured
    if _configured:
        return True
    if not instrumentation_enabled():
        return False

    from agent_framework.observability import configure_otel_providers

    if not any(os.getenv(var) for var in _EXPORTER_VARS):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = DEFAULT_OTLP_ENDPOINT

    kwargs: dict[str, object] = {"otlp_timeout": 2}
    if service_name:
        kwargs["service_name"] = service_name

    # LIVE only: Azure Monitor goes in as extra exporters on the SAME provider.
    #
    # The obvious alternative - configure OTLP here, then call
    # `configure_azure_monitor()` afterwards - does not work, and fails quietly.
    # OpenTelemetry allows the global provider to be set once; the second call
    # logs "Overriding of current TracerProvider is not allowed" and is ignored.
    # Its HTTP auto-instrumentation still ships a few request spans to Azure, so
    # traces *appear* in Application Insights - but every governance.policy span,
    # and every invoke_agent / chat / execute_tool span, goes only to the local
    # collector. Verified: 7 spans reached Application Insights, none of them the
    # ones the talk is about.
    connection_string = _application_insights_connection_string()
    if connection_string:
        kwargs["exporters"] = _azure_monitor_exporters(connection_string)

    configure_otel_providers(**kwargs)

    _configured = recording()
    return _configured


def _application_insights_connection_string() -> str | None:
    """The Foundry project's Application Insights, LIVE only.

    MOCK never looks. It is offline by promise, not by accident, so it does not
    reach out to Azure even when a connection string happens to be configured.
    """
    from governance import settings

    if settings.demo_mode() != "live":
        return None
    explicit = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if explicit:
        return explicit
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        return None
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import AzureCliCredential

        with AIProjectClient(endpoint=endpoint, credential=AzureCliCredential()) as client:
            return client.telemetry.get_application_insights_connection_string() or None
    except Exception:  # noqa: BLE001 - no App Insights is the common case
        return None


def _azure_monitor_exporters(connection_string: str) -> list:
    from azure.monitor.opentelemetry.exporter import (
        AzureMonitorLogExporter,
        AzureMonitorMetricExporter,
        AzureMonitorTraceExporter,
    )

    return [
        AzureMonitorTraceExporter(connection_string=connection_string),
        AzureMonitorLogExporter(connection_string=connection_string),
        AzureMonitorMetricExporter(connection_string=connection_string),
    ]


def azure_monitor_active() -> bool:
    """Is an Azure Monitor exporter genuinely attached to the live provider?

    Looks for the exporter itself. Checking "is any span processor installed"
    was fooled by the local OTLP exporter and reported Azure as working when it
    was not.
    """
    provider = trace.get_tracer_provider()
    processors = getattr(getattr(provider, "_active_span_processor", None),
                         "_span_processors", ())
    return any(
        type(getattr(p, "span_exporter", None)).__name__ == "AzureMonitorTraceExporter"
        for p in processors
    )


def recording() -> bool:
    """Is a real tracer provider installed, i.e. do spans have trace ids?"""
    provider = trace.get_tracer_provider()
    return type(provider).__name__ != "ProxyTracerProvider"


def tracer():
    return trace.get_tracer(_TRACER_NAME)


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format_trace_id(context.trace_id)


def span_ids(span: Span | None = None) -> tuple[str | None, str | None]:
    context = (span or trace.get_current_span()).get_span_context()
    if not context.is_valid:
        return None, None
    return format_trace_id(context.trace_id), format_span_id(context.span_id)


@contextlib.contextmanager
def root_span(name: str) -> Iterator[Span]:
    """A span to hang a whole scenario from, so an act has one trace id."""
    with tracer().start_as_current_span(name, kind=SpanKind.CLIENT) as span:
        yield span


@contextlib.contextmanager
def policy_span(
    *,
    agent: str,
    entra_agent_id: str | None,
    target: str,
    kind: str,
    classification: str | None,
    engine: str,
    mode: str,
    delegation_depth: int = 0,
    delegation_chain: tuple[str, ...] = (),
) -> Iterator[Span]:
    """The governance decision span.

    Opened before the policy is evaluated and closed after the decision is
    known, so a denial is a span with a decision on it rather than an absence.
    """
    attributes = {
        ATTR_AGENT: agent,
        ATTR_TARGET: target,
        ATTR_KIND: kind,
        ATTR_ENGINE: engine,
        ATTR_MODE: mode,
        ATTR_DELEGATION_DEPTH: delegation_depth,
    }
    if entra_agent_id:
        attributes[ATTR_AGENT_ID] = entra_agent_id
    if classification:
        attributes[ATTR_CLASSIFICATION] = classification
    if delegation_chain:
        attributes[ATTR_DELEGATION_CHAIN] = " -> ".join(delegation_chain)

    with tracer().start_as_current_span(
        f"governance.policy {target}", kind=SpanKind.INTERNAL, attributes=attributes
    ) as span:
        yield span


def record_decision(span: Span, decision: str, rule_id: str, reason: str) -> None:
    span.set_attribute(ATTR_DECISION, decision)
    span.set_attribute(ATTR_RULE_ID, rule_id)
    span.set_attribute(ATTR_REASON, reason)
    # A denial is a normal, correct outcome of a working control, so it is not
    # a span error. It is findable by attribute, which is what an auditor wants.
    span.add_event(
        "governance.decision",
        {ATTR_DECISION: decision, ATTR_RULE_ID: rule_id, ATTR_REASON: reason},
    )


def inject_context(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """W3C trace context for the outbound A2A hop."""
    carrier = carrier if carrier is not None else {}
    propagate.inject(carrier)
    return carrier


@contextlib.contextmanager
def extracted_context(carrier: dict[str, str]) -> Iterator[None]:
    """Continue the caller's trace on the receiving side of an A2A hop."""
    from opentelemetry.context import attach, detach

    token = attach(propagate.extract(carrier))
    try:
        yield
    finally:
        detach(token)


def flush(timeout_millis: int = 5000) -> bool:
    """Push everything buffered to the collector, now.

    The batch span processor exports on a timer, so a short act can finish and
    the process exit before its spans leave. On stage that means clicking the
    trace id the act just printed and finding nothing there. Acts call this
    before they print their closing summary.
    """
    flushed = False
    for getter in (trace.get_tracer_provider, _meter_provider, _logger_provider):
        provider = getter()
        force_flush = getattr(provider, "force_flush", None)
        if callable(force_flush):
            try:
                force_flush(timeout_millis)
                flushed = True
            except Exception:  # pragma: no cover - exporter/collector specific
                continue
    return flushed


def _meter_provider():
    from opentelemetry import metrics

    return metrics.get_meter_provider()


def _logger_provider():
    from opentelemetry import _logs

    return _logs.get_logger_provider()
