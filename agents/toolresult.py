"""Render an Agent Framework tool result as plain text.

`AIFunction.invoke` returns provider-shaped content. The acts only ever want one
short line on a projector, so this flattens whatever came back to text without
the caller having to know the content model.
"""

from __future__ import annotations

import json
from typing import Any


def as_text(result: Any, *, limit: int | None = None) -> str:
    text = _unwrap_structured(_flatten(result).strip())
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _unwrap_structured(text: str) -> str:
    """MCP structured content wraps a scalar return in ``{"result": ...}``.

    That wrapper is an artefact of the transport, not something an audience
    needs to read, so unwrap a lone ``result`` key back to its value.
    """
    if not (text.startswith("{") and '"result"' in text):
        return text
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if isinstance(payload, dict) and set(payload) == {"result"}:
        inner = payload["result"]
        return inner if isinstance(inner, str) else json.dumps(inner, indent=2)
    return text


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(value, (list, tuple)):
        parts = [p for p in (_flatten(v) for v in value) if p]
        return "\n".join(parts)
    for attr in ("value", "content", "output"):
        inner = getattr(value, attr, None)
        if inner is not None and inner is not value:
            flattened = _flatten(inner)
            if flattened:
                return flattened
    return str(value)
