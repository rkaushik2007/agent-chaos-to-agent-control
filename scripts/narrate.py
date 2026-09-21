"""Stage narration helpers.

Every act prints the same shape: a title card, a live trace of what the agents
attempted, then a summary that says what happened and what it means. Colours are
chosen for a projector: strong foreground on the terminal's own background, no
dim text, no 8-bit greys.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Windows terminals still default to a legacy code page, which turns rich's box
# characters and its truncation ellipsis into replacement glyphs on a projector.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - non-reconfigurable stream
        pass

def _build_console() -> Console:
    """A console that still looks like the demo when stdout is not a terminal.

    rich turns colour off and clamps to 79 columns whenever stdout is not a TTY,
    which is exactly what PyCharm's run console is unless "Emulate terminal in
    output console" is ticked. For a demo whose whole point is a colour-coded
    ALLOW / DENY / APPROVE column, losing colour is losing the demo.

    PyCharm sets `PYCHARM_HOSTED=1` in every run configuration, so that alone is
    enough to know colour is safe. `DEMO_FORCE_COLOR` and `DEMO_CONSOLE_WIDTH`
    are the manual overrides for any other host - CI logs, tmux, a captured
    recording.
    """
    requested = (
        os.getenv("DEMO_FORCE_COLOR")
        or os.getenv("FORCE_COLOR")
        or os.getenv("PYCHARM_HOSTED")
    )
    force_terminal = (
        True if requested and requested.strip().lower() not in ("0", "false", "no", "off")
        else None
    )
    try:
        width = int(os.getenv("DEMO_CONSOLE_WIDTH", "")) or None
    except ValueError:
        width = None
    # A forced terminal with no width of its own would fall back to 80 and wrap
    # every table, so give it something a projector can use.
    if force_terminal and width is None and not sys.stdout.isatty():
        width = 120

    # Two Windows-specific things have to be switched off together, or forcing a
    # terminal changes nothing at all:
    #
    #   color_system   defaults to "windows", which paints with Win32 console
    #                  calls instead of escape sequences
    #   legacy_windows detected as True, which makes rich strip ANSI on its way
    #                  out and use that Win32 renderer
    #
    # Either one left alone and the output is still completely colourless once
    # it is redirected into a host like PyCharm's run console. Both off and the
    # escape sequences PyCharm understands actually arrive.
    extra: dict[str, object] = {}
    if force_terminal:
        extra["color_system"] = "truecolor"
        extra["legacy_windows"] = False

    return Console(
        highlight=False,
        soft_wrap=False,
        force_terminal=force_terminal,
        width=width,
        **extra,
    )


console = _build_console()

DECISION_STYLE = {
    "allow": "bold green",
    "deny": "bold red",
    "approve": "bold yellow",
    "approved": "bold green",
    "rejected": "bold red",
    "timeout": "bold red",
    "blocked": "bold red",
    "ungoverned": "bold magenta",
}

_ACT_COLOUR = {1: "red", 2: "cyan", 3: "yellow", 4: "green"}


def act_title(number: int, name: str, subtitle: str) -> None:
    colour = _ACT_COLOUR.get(number, "white")
    console.print()
    console.print(
        Panel(
            Text.assemble(
                (f"ACT {number}  ", f"bold {colour}"),
                (name.upper(), "bold white"),
                ("\n", ""),
                (subtitle, "white"),
            ),
            border_style=colour,
            padding=(1, 3),
        )
    )


def step(text: str) -> None:
    console.print(f"[bold white]->[/bold white] {text}")


def detail(text: str) -> None:
    console.print(f"   {text}")


def decision_line(agent: str, tool: str, decision: str, reason: str, rule_id: str = "") -> None:
    style = DECISION_STYLE.get(decision.lower(), "white")
    rule = f" [white]({rule_id})[/white]" if rule_id else ""
    console.print(
        f"   [bold white]{agent:<14}[/bold white] "
        f"[white]{tool:<16}[/white] "
        f"[{style}]{decision.upper():<9}[/{style}]{rule}  {reason}"
    )


def table(title: str, columns: list[str]) -> Table:
    t = Table(title=title, title_style="bold white", header_style="bold white",
              border_style="white", padding=(0, 1))
    for c in columns:
        t.add_column(c)
    return t


def show(renderable) -> None:
    console.print(renderable)


def summary(lines: list[str], *, closer: str | None = None, colour: str = "white") -> None:
    body = Text()
    for i, line in enumerate(lines):
        if i:
            body.append("\n")
        body.append("* ", style=f"bold {colour}")
        body.append(line, style="white")
    if closer:
        body.append("\n\n")
        body.append(closer, style=f"bold {colour}")
    console.print()
    console.print(Panel(body, title="[bold white]What just happened[/bold white]",
                        border_style=colour, padding=(1, 3)))
    console.print()


def warn(text: str) -> None:
    console.print(f"[bold yellow]![/bold yellow] {text}")


def error(text: str) -> None:
    console.print(f"[bold red]x[/bold red] {text}")
