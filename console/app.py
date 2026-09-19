"""The governance console.

Four panels, server-rendered, no JavaScript build step: the agent inventory, a
live decision feed, the approval queue and links into the trace UI.

Everything is sized for a projector - 20px body, 32px headings, light theme,
high contrast - because a governance console that nobody at the back can read is
a governance console nobody uses.

htmx is vendored in `console/static/` rather than loaded from a CDN. MOCK is
meant to work with the network unplugged, and a demo that quietly needs the
internet is a demo that fails in the one room where it matters.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from governance import settings
from governance.approvals import approval_queue
from governance.audit import audit_store
from governance.events import bus
from governance.registry import registry

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="Helix Agent Governance Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------

def inventory_rows() -> list[dict]:
    """The registry, plus any agent the audit store has seen that is not in it.

    An unregistered agent showing up in the feed is the single most useful thing
    this console can tell an operator, so it is a row in the inventory rather
    than something you have to notice scrolling past.
    """
    reg = registry()
    rows = [
        {
            "id": record.id,
            "display_name": record.display_name,
            "business_unit": record.business_unit,
            "owner": record.owner,
            "identity": record.identity_label,
            "clearance": sorted(record.data_clearance),
            "tools": sorted(record.allowed_tools),
            "peers": list(record.allowed_peers),
            "registered": True,
        }
        for record in reg.agents.values()
    ]

    seen = {row.agent for row in audit_store().recent(500)}
    for agent_id in sorted(seen - set(reg.agents)):
        rows.append(
            {
                "id": agent_id,
                "display_name": "UNREGISTERED",
                "business_unit": "none",
                "owner": "nobody",
                "identity": "self-asserted",
                "clearance": [],
                "tools": [],
                "peers": [],
                "registered": False,
            }
        )
    return rows


def decision_rows(limit: int = 40) -> list[dict]:
    base = settings.trace_ui_base_url()
    return [
        {
            "id": entry.id,
            "at": (entry.ts or "")[11:19],
            "agent": entry.agent,
            "identity": entry.entra_agent_id or "",
            "kind": entry.kind,
            "target": entry.target,
            "outcome": entry.decision,
            "rule_id": entry.rule_id,
            "reason": entry.reason,
            "classification": entry.classification or "",
            "trace_id": entry.trace_id or "",
            "short_trace": entry.short_trace,
            "trace_url": f"{base}/traces/detail/{entry.trace_id}" if entry.trace_id else "",
        }
        for entry in audit_store().recent(limit)
    ]


def approval_rows() -> list[dict]:
    return [request.as_dict() for request in approval_queue().pending()]


def _context(request: Request) -> dict:
    # Starlette receives `request` as the first argument to TemplateResponse and
    # injects it into the template context itself, so it is not repeated here.
    counts = audit_store().counts_by_decision()
    return {
        "mode": settings.demo_mode(),
        "engine": settings.governance_engine(),
        "trace_ui": settings.trace_ui_base_url(),
        "approval_timeout": int(settings.approval_timeout_seconds()),
        "inventory": inventory_rows(),
        "decisions": decision_rows(),
        "approvals": approval_rows(),
        "counts": counts,
        "total": sum(counts.values()),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _context(request))


@app.get("/panels/inventory", response_class=HTMLResponse)
async def panel_inventory(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_inventory.html", _context(request))


@app.get("/panels/decisions", response_class=HTMLResponse)
async def panel_decisions(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_decisions.html", _context(request))


@app.get("/panels/approvals", response_class=HTMLResponse)
async def panel_approvals(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_approvals.html", _context(request))


@app.post("/approvals/{request_id}/approve", response_class=HTMLResponse)
async def approve(request: Request, request_id: str,
                  approver: str = Form(default="console operator")) -> HTMLResponse:
    approval_queue().resolve(request_id, approved=True, resolved_by=approver)
    await asyncio.sleep(0.05)  # let the waiting act finish recording the outcome
    return templates.TemplateResponse(request, "_approvals.html", _context(request))


@app.post("/approvals/{request_id}/reject", response_class=HTMLResponse)
async def reject(request: Request, request_id: str,
                 approver: str = Form(default="console operator")) -> HTMLResponse:
    approval_queue().resolve(request_id, approved=False, resolved_by=approver)
    await asyncio.sleep(0.05)
    return templates.TemplateResponse(request, "_approvals.html", _context(request))


@app.get("/trace/{trace_id}")
async def open_trace(trace_id: str) -> RedirectResponse:
    """Hand off to whichever trace UI is configured.

    The Aspire Dashboard and Jaeger disagree about URL shape, so the console
    owns the mapping rather than baking one into every row.
    """
    base = settings.trace_ui_base_url()
    if "16686" in base:  # Jaeger
        return RedirectResponse(f"{base}/trace/{trace_id}")
    return RedirectResponse(f"{base}/traces/detail/{trace_id}")


@app.get("/events")
async def events(request: Request) -> EventSourceResponse:
    """The live feed.

    Decisions are made on the acts' event loop and the console runs on its own,
    so the bus hands each subscriber its items through `call_soon_threadsafe`.
    """

    async def stream():
        # An open SSE stream is the normal state on stage: the presenter leaves
        # the console up while the act finishes. When the act's server then
        # shuts down, this task is cancelled mid-yield, and without catching it
        # uvicorn prints "Exception in ASGI application" and a traceback over
        # the closing summary. A cancelled stream is a browser tab closing, not
        # a fault.
        try:
            async for event in bus().subscribe():
                if await request.is_disconnected():
                    break
                yield {
                    "event": event.kind,
                    "data": json.dumps({"at": event.at, **event.payload}, default=str),
                }
        except (asyncio.CancelledError, GeneratorExit):
            return

    return EventSourceResponse(stream())


@app.get("/api/decisions")
async def api_decisions(limit: int = 40) -> list[dict]:
    """For the rehearsal harness and anyone who wants the data, not the page."""
    return decision_rows(limit)


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mode": settings.demo_mode(),
        "engine": settings.governance_engine(),
        "decisions": audit_store().count(),
        "pending_approvals": len(approval_queue().pending()),
    }
