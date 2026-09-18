"""Stage narration helpers.

Every act prints the same shape: a title card, a live trace of what the agents
attempted, then a summary that says what happened and what it means. Colours are
chosen for a projector: strong foreground on the terminal's own background, no
dim text, no 8-bit greys.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(highlight=False, soft_wrap=False)

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
