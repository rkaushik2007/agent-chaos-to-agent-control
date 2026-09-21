"""Start everything the Foundry toolbox needs to reach, in one command.

    uv run python infra/start_live_tools.py

A Foundry toolbox does not host your tools - it holds a *reference* to an MCP
server and calls it. So while you present LIVE, three things must be running on
this machine:

    clinical-tools, catalogue v1   127.0.0.1:8765   (4 tools)
    clinical-tools, catalogue v2   127.0.0.1:8766   (5 tools)
    the dev tunnel                 publishes both to Foundry

Two servers rather than one because a toolbox *version* holds tool sources, and
Foundry reads the tool list from the server itself. "v1 has four tools" means
the server behind v1 really exposes four.

Leave this running in its own terminal. Ctrl-C stops all three.

The toolbox *object* stays visible in Foundry whether or not this is running -
only calling its tools needs it. MOCK needs none of this.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from governance import settings  # noqa: E402

SERVERS = (("v1", 8765), ("v2", 8766))
TUNNEL = os.getenv("DEVTUNNEL_NAME", "helix-trial-tools")


def find_devtunnel() -> str:
    found = shutil.which("devtunnel")
    if found:
        return found
    # winget installs a shim that only appears on PATH in a new shell.
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    for candidate in winget.rglob("devtunnel.exe") if winget.exists() else ():
        return str(candidate)
    raise SystemExit(
        "devtunnel is not installed.\n"
        "  winget install Microsoft.devtunnel\n"
        "  devtunnel user login -w -e\n"
        "See docs/LIVE_SETUP.md."
    )


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def main() -> int:
    # Line-buffered, so status appears immediately in PyCharm's run console or a
    # log file rather than all at once when the process exits.
    sys.stdout.reconfigure(line_buffering=True)
    settings.load_env()
    devtunnel = find_devtunnel()

    busy = [port for _, port in SERVERS if listening(port)]
    if busy:
        print(f"Port(s) {busy} already in use - is this already running? Stop it first.")
        return 1

    children: list[subprocess.Popen] = []
    try:
        for version, port in SERVERS:
            env = {**os.environ,
                   "CLINICAL_TOOLS_VERSION": version,
                   "CLINICAL_TOOLS_PORT": str(port)}
            children.append(subprocess.Popen(
                [sys.executable, "-m", "mcp_servers.clinical_tools.server"],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))

        for version, port in SERVERS:
            for _ in range(150):
                if listening(port):
                    break
                time.sleep(0.1)
            else:
                print(f"clinical-tools {version} did not start on {port}")
                return 1
            print(f"  clinical-tools {version:<3} listening on 127.0.0.1:{port}")

        tunnel = subprocess.Popen(
            [devtunnel, "host", TUNNEL],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        children.append(tunnel)

        urls: list[str] = []
        deadline = time.time() + 45
        assert tunnel.stdout is not None
        while time.time() < deadline:
            line = tunnel.stdout.readline()
            if not line:
                if tunnel.poll() is not None:
                    print("devtunnel exited. Is the tunnel created and are you logged in?")
                    print(f"  devtunnel show {TUNNEL}")
                    return 1
                continue
            if "Connect via browser:" in line:
                urls.append(line.split("Connect via browser:", 1)[1].strip())
            if "Ready to accept connections" in line:
                break

        print(f"  tunnel {TUNNEL} is up")
        for url in urls:
            print(f"    {url}/mcp")
        print()
        print("Foundry can now reach the tools. Run LIVE in another terminal:")
        print("  DEMO_MODE=live uv run demo act2")
        print()
        print("Ctrl-C here stops the servers and the tunnel.")

        while all(child.poll() is None for child in children):
            time.sleep(1)
        print("A process exited unexpectedly; stopping everything.")
        return 1
    except KeyboardInterrupt:
        print("\nstopping...")
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                if os.name == "nt":
                    child.terminate()
                else:
                    child.send_signal(signal.SIGINT)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    raise SystemExit(main())
