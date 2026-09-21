"""Process-wide demo settings.

`DEMO_MODE` and `GOVERNANCE_ENGINE` are the only two switches in the demo.
Nothing in `governance/` branches on them except the three factory functions
that choose an identity provider, a tool source and a model client. The policy
evaluator, the enforcement seam, the A2A gateway, the audit store and the
telemetry are the same code in both modes - that is the claim the talk makes,
so it has to be structurally true.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"

AGENTS_FILE = CONFIG_DIR / "agents.yaml"
POLICY_FILE = CONFIG_DIR / "policy.yaml"
TOOLBOX_FILE = CONFIG_DIR / "toolbox.yaml"

DemoMode = Literal["mock", "live"]
GovernanceEngine = Literal["middleware", "hooks"]

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_env() -> None:
    """Load `.env` if present. Never required - MOCK runs with no env at all."""
    load_dotenv(REPO_ROOT / ".env", override=False)


def demo_mode() -> DemoMode:
    value = os.getenv("DEMO_MODE", "mock").strip().lower()
    if value not in ("mock", "live"):
        raise ValueError(f"DEMO_MODE must be 'mock' or 'live', got {value!r}")
    return value  # type: ignore[return-value]


def governance_engine() -> GovernanceEngine:
    value = os.getenv("GOVERNANCE_ENGINE", "middleware").strip().lower()
    if value not in ("middleware", "hooks"):
        raise ValueError(f"GOVERNANCE_ENGINE must be 'middleware' or 'hooks', got {value!r}")
    return value  # type: ignore[return-value]


def audit_db_path() -> Path:
    return Path(os.getenv("AUDIT_DB", str(REPO_ROOT / "audit.db")))


def trace_ui_base_url() -> str:
    """Where the console links a decision's trace id. Aspire Dashboard by default."""
    return os.getenv("TRACE_UI_URL", "http://localhost:18888").rstrip("/")


# Not 8000. That is the default for half the dev servers ever written, so it is
# the port most likely to be held by something else on the machine you present
# from - which is exactly when you cannot afford to go hunting. 8787 is
# uncommon, and easy to read out loud.
DEFAULT_CONSOLE_PORT = 8787


def console_port() -> int:
    """The port the governance console prefers. `CONSOLE_PORT` overrides it."""
    try:
        return int(os.getenv("CONSOLE_PORT", "") or DEFAULT_CONSOLE_PORT)
    except ValueError:
        return DEFAULT_CONSOLE_PORT


def approval_timeout_seconds() -> float:
    return float(os.getenv("APPROVAL_TIMEOUT_SECONDS", "60"))


def expand_env(value):
    """Expand ``${VAR}`` references in loaded config.

    An unset variable expands to ``None`` rather than the literal ``${VAR}``.
    In MOCK nothing needs these, and in LIVE a missing value must surface as a
    clear "not configured" rather than as a string that looks like a real id.
    """
    if isinstance(value, str):
        match = _ENV_REF.fullmatch(value.strip())
        if match:
            return os.getenv(match.group(1)) or None
        return _ENV_REF.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value
