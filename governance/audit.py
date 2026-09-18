"""The audit store.

Every decision the governance layer makes is written here before the caller is
told the answer. Act 1's closing line is "no trace"; this is the file that makes
that untrue from act 3 onward.

SQLite because it is one file, needs no service, survives the process, and can
be opened by anyone in the audience afterwards with a tool they already have.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from governance import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT    NOT NULL,
    trace_id          TEXT,
    span_id           TEXT,
    agent             TEXT    NOT NULL,
    entra_agent_id    TEXT,
    kind              TEXT    NOT NULL,   -- 'tool' | 'a2a'
    target            TEXT    NOT NULL,   -- tool name, or peer id for a2a
    action            TEXT,
    classification    TEXT,
    decision          TEXT    NOT NULL,   -- allow | deny | approve | approved | rejected | timeout
    rule_id           TEXT    NOT NULL,
    reason            TEXT    NOT NULL,
    engine            TEXT,
    mode              TEXT,
    resolved_by       TEXT,
    delegation_chain  TEXT,
    arguments         TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_trace ON decisions (trace_id);
"""


@dataclass
class AuditEntry:
    agent: str
    kind: str
    target: str
    decision: str
    rule_id: str
    reason: str
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    trace_id: str | None = None
    span_id: str | None = None
    entra_agent_id: str | None = None
    action: str | None = None
    classification: str | None = None
    engine: str | None = None
    mode: str | None = None
    resolved_by: str | None = None
    delegation_chain: str | None = None
    arguments: str | None = None
    id: int | None = None

    @property
    def short_trace(self) -> str:
        return (self.trace_id or "")[:16]


class AuditStore:
    """Append-only in practice; `reset()` exists only for `demo reset`."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or settings.audit_db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def record(self, entry: AuditEntry) -> AuditEntry:
        payload = asdict(entry)
        payload.pop("id", None)
        columns = ", ".join(payload)
        placeholders = ", ".join(f":{k}" for k in payload)
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO decisions ({columns}) VALUES ({placeholders})", payload
            )
            self._conn.commit()
            entry.id = int(cursor.lastrowid or 0)
        return entry

    def update_resolution(self, entry_id: int, decision: str, resolved_by: str,
                          reason: str | None = None) -> None:
        """An `approve` becomes `approved`, `rejected` or `timeout` once answered."""
        with self._lock:
            if reason is None:
                self._conn.execute(
                    "UPDATE decisions SET decision = ?, resolved_by = ? WHERE id = ?",
                    (decision, resolved_by, entry_id),
                )
            else:
                self._conn.execute(
                    "UPDATE decisions SET decision = ?, resolved_by = ?, reason = ? WHERE id = ?",
                    (decision, resolved_by, reason, entry_id),
                )
            self._conn.commit()

    def recent(self, limit: int = 50) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_from_row(row) for row in rows]

    def by_trace(self, trace_id: str) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions WHERE trace_id = ? ORDER BY id", (trace_id,)
            ).fetchall()
        return [_from_row(row) for row in rows]

    def counts_by_decision(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT decision, COUNT(*) AS n FROM decisions GROUP BY decision"
            ).fetchall()
        return {row["decision"]: int(row["n"]) for row in rows}

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
        return int(row["n"])

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM decisions")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _from_row(row: sqlite3.Row) -> AuditEntry:
    data = dict(row)
    return AuditEntry(**data)


def dump_arguments(arguments: Iterable | dict | None) -> str | None:
    if arguments is None:
        return None
    try:
        return json.dumps(arguments, default=str)[:2000]
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(arguments)[:2000]


_STORE: AuditStore | None = None
_STORE_LOCK = threading.Lock()


def audit_store() -> AuditStore:
    """The process-wide store."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = AuditStore()
        return _STORE


def use_store(store: AuditStore | None) -> None:
    """Point the process at a different store. Used by the tests."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store
