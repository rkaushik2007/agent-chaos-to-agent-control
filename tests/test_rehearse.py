"""Milestone 6: the end-to-end rehearsal.

This is the slowest test in the suite by a wide margin, and it earns it: it runs
all four acts exactly as the stage does and checks every decision. If it is
green, the demo is green.

It also checks the harness itself. A pre-flight check that cannot fail is worse
than no pre-flight check, because it is believed.
"""

from __future__ import annotations

import os

import pytest

from scripts import rehearse as rehearse_module
from scripts.acts import act1
from scripts.rehearse import EXPECTED, rehearse


@pytest.fixture(autouse=True)
def restore_environment():
    """The acts set process-wide switches. Put them back."""
    keys = (
        "DEMO_MODE", "GOVERNANCE_ENGINE", "ENABLE_INSTRUMENTATION",
        "ENABLE_SENSITIVE_DATA", "APPROVAL_TIMEOUT_SECONDS", "ACT4_APPROVAL_TIMEOUT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    before = {key: os.environ.get(key) for key in keys}
    os.environ["ACT4_APPROVAL_TIMEOUT"] = "2"
    # The rest of the suite runs with telemetry off. The rehearsal must not:
    # act 4 asserts it produced a real trace id, which is the whole point of the
    # act. No collector is needed - spans record and carry ids whether or not
    # anything is listening, and the failed export is silenced.
    os.environ["ENABLE_INSTRUMENTATION"] = "true"
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    act1.cleanup()


@pytest.mark.slow
async def test_all_four_acts_run_and_every_decision_is_as_scripted():
    assert await rehearse() == 0


@pytest.mark.slow
async def test_the_rehearsal_actually_fails_when_a_decision_changes(monkeypatch):
    """Proof that the gate gates.

    The expectation for act 1 is bent to something the demo does not do. If the
    rehearsal still passes, it is not checking anything.
    """
    bent = dict(EXPECTED)
    bent[1] = [("trial_ops", "update_case", "deny")]  # act 1 has no governance
    monkeypatch.setattr(rehearse_module, "EXPECTED", bent)

    assert await rehearse() == 1
