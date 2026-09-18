"""`demo` - the one entry point for the whole show.

    uv run demo act1        Chaos
    uv run demo act2        Identity + Toolbox
    uv run demo act3        Enforcement
    uv run demo act4        Visibility
    uv run demo rehearse    All four, non-interactive, asserting every decision
    uv run demo reset       Wipe the audit DB, reseed data, drop the chaos dotfile
    uv run demo console     Serve the governance console
    uv run demo seed        Regenerate data/*.json

The Makefile targets are one-line wrappers over these, so the commands are the
same whether or not you have `make`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from governance import settings
from scripts import narrate
from scripts.logging_setup import quiet, quiet_asyncio


def _run_act(number: int) -> Callable[[argparse.Namespace], int]:
    def runner(args: argparse.Namespace) -> int:
        module = __import__(f"scripts.acts.act{number}", fromlist=["run"])

        async def go():
            quiet_asyncio()
            return await module.run(interactive=not args.non_interactive)

        result = asyncio.run(go())
        return 0 if result is not None else 1

    return runner


def _cmd_seed(args: argparse.Namespace) -> int:
    from scripts.seed_data import write

    for name, path in write().items():
        narrate.console.print(f"  reseeded [bold]{name}[/bold] -> {path}")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    from scripts.acts import act1

    db = settings.audit_db_path()
    if db.exists():
        db.unlink()
        narrate.console.print(f"  removed audit store [bold]{db}[/bold]")
    else:
        narrate.console.print(f"  audit store already clean ({db})")
    act1.cleanup()
    narrate.console.print("  removed .env.chaos")
    _cmd_seed(args)
    narrate.console.print("[bold green]Reset complete. Ready to rehearse.[/bold green]")
    return 0


def _cmd_rehearse(args: argparse.Namespace) -> int:
    from scripts.rehearse import rehearse

    return asyncio.run(rehearse())


def _cmd_console(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("console.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from scripts.doctor import doctor

    return doctor()


def main(argv: list[str] | None = None) -> int:
    settings.load_env()
    quiet()

    parser = argparse.ArgumentParser(prog="demo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--non-interactive", action="store_true",
                        help="never wait for a human; auto-resolve approvals")
    sub = parser.add_subparsers(dest="command", required=True)

    for n, help_text in (
        (1, "Chaos - governance off, everything succeeds"),
        (2, "Identity + Toolbox - agents get identities and one tool endpoint"),
        (3, "Enforcement - policy middleware and the A2A gateway"),
        (4, "Visibility - one trace, end to end"),
    ):
        p = sub.add_parser(f"act{n}", help=help_text)
        p.set_defaults(func=_run_act(n))

    sub.add_parser("rehearse", help="run all four acts and assert every decision").set_defaults(
        func=_cmd_rehearse)
    sub.add_parser("reset", help="wipe the audit DB, reseed data").set_defaults(func=_cmd_reset)
    sub.add_parser("seed", help="regenerate data/*.json").set_defaults(func=_cmd_seed)
    sub.add_parser("doctor", help="check the environment before you go on stage").set_defaults(
        func=_cmd_doctor)

    c = sub.add_parser("console", help="serve the governance console")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8000)
    c.set_defaults(func=_cmd_console)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        narrate.warn("interrupted")
        return 130
    except ModuleNotFoundError as exc:
        narrate.error(f"{exc}")
        narrate.warn("That part of the demo is not built yet. See the milestones in README.md.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
