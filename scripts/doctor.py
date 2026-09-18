"""`uv run demo doctor` - the pre-talk environment check.

Answers one question: if I run the four acts right now, will they work? It never
mutates anything, so it is safe to run between rehearsals and safe to run on
stage while someone is still walking to the lectern.
"""

from __future__ import annotations

import importlib
import os
import socket
from pathlib import Path

from governance import settings
from scripts import narrate

OK = "[bold green]ok[/bold green]"
WARN = "[bold yellow]warn[/bold yellow]"
FAIL = "[bold red]fail[/bold red]"


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _module(name: str) -> tuple[str, str]:
    try:
        importlib.import_module(name)
        return OK, "importable"
    except Exception as exc:
        return FAIL, f"{type(exc).__name__}: {exc}"


def doctor() -> int:
    settings.load_env()
    mode = settings.demo_mode()
    engine = settings.governance_engine()

    table = narrate.table("Pre-flight", ["Check", "Status", "Detail"])
    failures = 0

    table.add_row("DEMO_MODE", OK, mode)
    table.add_row("GOVERNANCE_ENGINE", OK, engine)

    for name in ("agent_framework", "mcp.server.fastmcp", "agent_framework.a2a"):
        status, detail = _module(name)
        failures += status == FAIL
        table.add_row(name, status, detail)

    if engine == "hooks":
        status, detail = _module("agent_hooks")
        failures += status == FAIL
        table.add_row("agent_hooks (experimental)", status, detail)

    for name in ("protocols", "cases", "suppliers"):
        path = settings.DATA_DIR / f"{name}.json"
        if path.exists():
            table.add_row(f"data/{name}.json", OK, f"{path.stat().st_size:,} bytes")
        else:
            failures += 1
            table.add_row(f"data/{name}.json", FAIL, "missing - run `uv run demo seed`")

    for path in (settings.AGENTS_FILE, settings.POLICY_FILE, settings.TOOLBOX_FILE):
        rel = path.relative_to(settings.REPO_ROOT)
        if path.exists():
            table.add_row(str(rel), OK, "present")
        else:
            failures += 1
            table.add_row(str(rel), FAIL, "missing")

    db = settings.audit_db_path()
    table.add_row("audit store", OK,
                  f"{db} ({'exists' if Path(db).exists() else 'will be created'})")

    otlp = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    host_port = otlp.split("//", 1)[-1]
    host, _, port = host_port.partition(":")
    reachable = _port_open(host or "localhost", int(port or 4317))
    table.add_row("trace collector", OK if reachable else WARN,
                  f"{otlp} {'reachable' if reachable else 'not reachable - run `docker compose up -d`'}")

    ui = settings.trace_ui_base_url()
    ui_host, _, ui_port = ui.split("//", 1)[-1].partition(":")
    ui_up = _port_open(ui_host or "localhost", int(ui_port or 80))
    table.add_row("trace UI", OK if ui_up else WARN,
                  f"{ui} {'reachable' if ui_up else 'not reachable'}")

    if mode == "live":
        for var in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_MODEL_DEPLOYMENT_NAME"):
            value = os.getenv(var)
            if value:
                table.add_row(var, OK, value)
            else:
                failures += 1
                table.add_row(var, FAIL, "not set - LIVE cannot start")
        if not (os.getenv("TOOLBOX_ENDPOINT") or os.getenv("TOOLBOX_NAME")):
            failures += 1
            table.add_row("TOOLBOX_ENDPOINT / TOOLBOX_NAME", FAIL, "set one of them")
        status, detail = _module("agent_framework_foundry_hosting")
        failures += status == FAIL
        table.add_row("agent_framework_foundry_hosting", status, detail)

    narrate.show(table)

    if failures:
        narrate.error(f"{failures} blocking problem(s). Fix these before going on stage.")
        return 1
    narrate.console.print("[bold green]Ready.[/bold green] "
                          "Next: `uv run demo rehearse` must pass.")
    return 0
