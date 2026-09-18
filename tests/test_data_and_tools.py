"""Milestone 1: the synthetic data and the clinical-tools MCP server."""

from __future__ import annotations

import json
import re

import pytest

from agents.planner import SCENARIOS, Delegation, ToolCall, plan, scenario
from agents.toolresult import as_text
from mcp_servers.clinical_tools import server as tools
from mcp_servers.clinical_tools.runtime import clinical_tools_server
from scripts.seed_data import build


@pytest.fixture(autouse=True)
def _clean_tool_state():
    tools.reset_state()
    yield
    tools.reset_state()


def test_seed_is_deterministic():
    assert build() == build()


def test_seed_shapes():
    data = build()
    assert len(data["protocols"]) == 40
    assert len(data["cases"]) == 30
    assert len(data["suppliers"]) == 15
    assert [c["case_id"] for c in data["cases"]][:2] == ["AE-0001", "AE-0002"]


def test_cases_carry_no_personal_identifiers():
    """Synthetic only. Subject codes, never names, dates of birth or contacts."""
    blob = json.dumps(build()["cases"])
    assert re.search(r"\b(name|dob|date_of_birth|email|phone|address)\b", blob, re.I) is None
    for case in build()["cases"]:
        assert re.fullmatch(r"SUBJ-\d{4}", case["subject_id"])
        assert case["classification"] == "phi"


def test_every_tool_is_classified_in_the_catalogue():
    """A tool the catalogue does not classify cannot be governed, so it must not exist."""
    import yaml

    from governance.settings import TOOLBOX_FILE

    catalogue = yaml.safe_load(TOOLBOX_FILE.read_text(encoding="utf-8"))
    catalogued = set(catalogue["tools"])
    exposed = {"search_docs", "read_case", "update_case", "create_po", "lookup_supplier"}
    assert exposed == catalogued
    for name, spec in catalogue["tools"].items():
        assert spec["action"] in ("read", "write"), name
        assert spec["classification"] in ("internal", "phi", "financial"), name


def test_tools_behave():
    assert "HTX-" in tools.search_docs("unblinding")
    assert json.loads(tools.read_case("AE-0003"))["case_id"] == "AE-0003"
    assert "changed from" in tools.update_case("AE-0003", "status", "closed")
    assert "PO-5001" in tools.create_po("Vendor A Clinical Services", "shipper", 3)
    assert "SUP-" in tools.lookup_supplier("cold chain")


def test_writes_do_not_touch_the_committed_fixtures():
    tools.update_case("AE-0003", "status", "mutated")
    assert json.loads(tools.read_case("AE-0003"))["status"] == "mutated"
    tools.reset_state()
    assert json.loads(tools.read_case("AE-0003"))["status"] != "mutated"


def test_planner_scenarios_are_coherent():
    for key, sc in SCENARIOS.items():
        assert sc.key == key
        assert sc.steps, f"{key} has no steps"
        for step in sc.steps:
            assert isinstance(step, (ToolCall, Delegation))
    assert plan("trial_ops", scenario("chaos.trial_ops_updates_case").prompt)


async def test_mcp_server_serves_the_tools_and_filters():
    from agent_framework import MCPStreamableHTTPTool

    async with clinical_tools_server() as url:
        async with MCPStreamableHTTPTool(name="clinical-tools", url=url) as session:
            assert sorted(f.name for f in session.functions) == [
                "create_po", "lookup_supplier", "read_case", "search_docs", "update_case",
            ]
        async with MCPStreamableHTTPTool(
            name="clinical-tools", url=url, allowed_tools=["search_docs"]
        ) as filtered:
            assert [f.name for f in filtered.functions] == ["search_docs"]
            result = await filtered.functions[0].invoke(arguments={"query": "dosing"})
            assert "HTX-" in as_text(result)
