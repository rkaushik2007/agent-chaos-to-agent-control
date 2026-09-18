"""The `clinical-tools` MCP server.

One real MCP server, built with the official MCP Python SDK (FastMCP), serving
the four clinical tools the demo governs. It is deliberately naive: it performs
no authorisation of its own. That is the point of the talk - the tools do not
protect themselves, the governance layer protects them.

Data is loaded from `data/*.json` at import time. Writes mutate an in-process
overlay only, so every run starts from identical state and the committed
fixtures are never modified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

SERVER_NAME = "clinical-tools"


def _load(name: str) -> list[dict[str, Any]]:
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        raise RuntimeError(
            f"{path} is missing. Run `uv run demo reset` (or `uv run python scripts/seed_data.py`)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


PROTOCOLS = _load("protocols")
SUPPLIERS = _load("suppliers")
_CASES: dict[str, dict[str, Any]] = {c["case_id"]: c for c in _load("cases")}
_PURCHASE_ORDERS: list[dict[str, Any]] = []


def reset_state() -> None:
    """Restore the in-process overlay to the seeded fixtures."""
    global _CASES, _PURCHASE_ORDERS
    _CASES = {c["case_id"]: c for c in _load("cases")}
    _PURCHASE_ORDERS = []


def purchase_orders() -> list[dict[str, Any]]:
    return list(_PURCHASE_ORDERS)


def search_docs(query: str) -> str:
    """Search Helix protocol documents for a phrase. Classification: internal."""
    needle = query.strip().lower()
    hits = [
        d for d in PROTOCOLS
        if needle in d["text"].lower() or needle in d["section"].lower()
    ][:5]
    if not hits:
        return f"No protocol clauses matched {query!r}."
    return "\n".join(f"{d['doc_id']} [{d['section']}] {d['text']}" for d in hits)


def read_case(case_id: str) -> str:
    """Read one adverse event case record. Classification: phi."""
    case = _CASES.get(case_id.strip().upper())
    if case is None:
        return f"No case {case_id!r}."
    return json.dumps(case, indent=2)


def update_case(case_id: str, field: str, value: str) -> str:
    """Update one field on an adverse event case record. Classification: phi."""
    key = case_id.strip().upper()
    case = _CASES.get(key)
    if case is None:
        return f"No case {key!r}."
    if field not in case:
        return f"Case {key} has no field {field!r}."
    before = case[field]
    case[field] = value
    return f"Case {key}: {field} changed from {before!r} to {value!r}."


def create_po(vendor: str, item: str, qty: int) -> str:
    """Raise a purchase order for trial supplies. Classification: financial."""
    po_id = f"PO-{5000 + len(_PURCHASE_ORDERS) + 1}"
    _PURCHASE_ORDERS.append({"po_id": po_id, "vendor": vendor, "item": item, "qty": qty})
    return f"{po_id} raised: {qty} x {item} from {vendor}."


def lookup_supplier(query: str) -> str:
    """Look up an approved supplier by id or category. Classification: financial."""
    needle = query.strip().lower()
    hits = [
        s for s in SUPPLIERS
        if needle in s["supplier_id"].lower()
        or needle in s["name"].lower()
        or needle in s["category"].lower()
    ][:5]
    if not hits:
        return f"No supplier matched {query!r}."
    return "\n".join(
        f"{s['supplier_id']} {s['name']} ({s['category']}, {s['region']}, "
        f"{'approved' if s['approved'] else 'NOT APPROVED'})"
        for s in hits
    )


TOOLS = (search_docs, read_case, update_case, create_po, lookup_supplier)


def build_server() -> FastMCP:
    """Build a fresh server instance.

    A `FastMCP` caches its streamable-HTTP session manager, and a session
    manager can only be run once. Re-using one module-level instance therefore
    works for the first server start in a process and fails for every one after
    it - which is exactly what a test suite, and a rehearsal that runs four acts
    back to back, will do. So each start gets its own instance.
    """
    # Stateless: every request is self-contained. The default stateful mode
    # keeps a per-session task group alive with a 30-minute idle timeout, which
    # accumulates across the repeated server starts a rehearsal makes and
    # eventually leaves a server that accepts connections but never answers.
    # Nothing here needs session state, and MCP `_meta` trace propagation is
    # per-request, so statelessness costs the demo nothing.
    server = FastMCP(name=SERVER_NAME, stateless_http=True)
    for fn in TOOLS:
        server.tool()(fn)
    return server


if __name__ == "__main__":
    build_server().run(transport="streamable-http")
