"""Act 2 - Identity and Toolbox.

Two controls arrive. Every agent gets an identity it did not mint itself, and
every tool moves behind one endpoint that decides what each caller is shown.

Nothing is enforced yet - a tool an agent can see is still a tool it can call.
That is act 3. What act 2 buys is that there is now a subject to attribute
actions to, and a single place where tools are published.
"""

from __future__ import annotations

import time

import httpx

from agents.planner import ToolCall, scenario
from agents.toolresult import as_text
from governance.identity import IdentityError, MockIdentityProvider, identity_provider
from governance.registry import registry
from governance.settings import demo_mode
from governance.toolbox import tool_source_factory
from mcp_servers.clinical_tools.runtime import clinical_tools_server
from scripts import narrate
from scripts.acts.result import ActResult

GOVERNED = ("trial_ops", "safety_triage", "supply")
UNGOVERNED = "shadow_agent"


def bare(tool: str) -> str:
    """Foundry namespaces tools as `<server_label>___<tool>`.

    Governance strips that before evaluating policy; the narration strips it too,
    because the audience is being shown which *tool* each agent can see, not
    which source it happens to arrive from.
    """
    from governance.toolbox_live import strip_namespace

    return strip_namespace(tool)


async def _anonymous_tools(endpoint: str) -> list[str]:
    """What a caller with no identity gets. A plain request, no MCP client.

    Deliberately not an `MCPStreamableHTTPTool`: when the handshake is rejected -
    which is the whole point here - its lifecycle task is left pending and
    asyncio complains loudly over the narration.
    """
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(endpoint, json=request, headers=headers)
        if response.status_code >= 400:
            return []
        payload = response.json()
    except Exception:
        return []
    return sorted(t["name"] for t in payload.get("result", {}).get("tools", []))


async def run(*, interactive: bool = True) -> ActResult:
    result = ActResult(act=2, name="Identity + Toolbox")
    reg = registry()
    mode = demo_mode()
    identity = identity_provider(reg) if mode == "live" else MockIdentityProvider(reg)

    narrate.act_title(
        2,
        "Identity + Toolbox",
        "Every agent gets an identity it cannot mint itself.\n"
        "Every tool moves behind one endpoint that decides what each caller sees.",
    )

    # ---------------------------------------------------------------- registry
    narrate.step("The agent registry now exists. This is the inventory.")
    inventory = narrate.table(
        "config/agents.yaml",
        ["Agent / business unit / accountable owner", "Clearance", "Granted tools"],
    )
    for agent_id in GOVERNED:
        record = reg.require_agent(agent_id)
        inventory.add_row(
            f"[bold]{agent_id}[/bold]\n{record.business_unit}\n{record.owner}",
            "\n".join(sorted(record.data_clearance)),
            "\n".join(sorted(record.allowed_tools)),
        )
    inventory.add_row(
        f"[bold red]{UNGOVERNED}[/bold red]\n[red]no business unit[/red]\n"
        f"[bold red]owned by nobody[/bold red]",
        "[red]none[/red]", "[red]none[/red]",
    )
    narrate.show(inventory)

    # --------------------------------------------------------------- identity
    narrate.step(f"Issuing identities via the [bold]{identity.name}[/bold] provider")
    principals = {}
    for agent_id in GOVERNED:
        principal = identity.issue(agent_id)
        principals[agent_id] = principal
        ttl = int(principal.expires_at - time.time())
        narrate.detail(
            f"[bold]{agent_id:<14}[/bold] [green]{principal.identity_label}[/green]  "
            f"expires in {ttl}s  scopes: {len(principal.scopes)}"
        )
        narrate.detail(f"{'':<16}token {principal.token[:34]}...")
        result.record(agent_id, "identity", "issued", reason=principal.identity_label)

    try:
        identity.issue(UNGOVERNED)
    except IdentityError as exc:
        narrate.decision_line(UNGOVERNED, "identity", "deny", str(exc))
        result.record(UNGOVERNED, "identity", "deny", "REGISTRY-000", str(exc))
    else:  # pragma: no cover - would mean the registry check regressed
        raise AssertionError("an unregistered agent was issued an identity")
    print()

    async with clinical_tools_server() as upstream:
        # LocalToolbox in MOCK, the Foundry toolbox endpoint in LIVE.
        async with tool_source_factory()(upstream, identity, reg) as toolbox:
            # ------------------------------------------------------- toolbox
            narrate.step(f"One endpoint: [bold]{toolbox.endpoint}[/bold]")
            narrate.detail(
                f"serving version [bold]{toolbox.version}[/bold] -> "
                f"{', '.join(toolbox.published_tools)}"
            )
            narrate.detail("Agents connect here and nowhere else. "
                           "The endpoint filters; the client is not asked to.")
            print()

            narrate.step("What each agent sees when it calls tools/list")
            listing = narrate.table(
                f"tools/list at toolbox {toolbox.version}",
                ["Caller", "Identity presented", "Tools returned"],
            )
            for agent_id in GOVERNED:
                async with toolbox.session_for(principals[agent_id]) as session:
                    names = sorted(bare(f.name) for f in session.functions)
                listing.add_row(
                    f"[bold]{agent_id}[/bold]",
                    f"[green]{principals[agent_id].identity_label}[/green]",
                    ", ".join(names) or "[red]none[/red]",
                )
                result.record(agent_id, "tools/list", "filtered", reason=",".join(names))

            # The shadow agent has no identity, so this is all it can do. The
            # local toolbox answers with an empty list; Foundry rejects the
            # request outright. Both are "you get nothing", and neither should
            # take the act down.
            anon_names = await _anonymous_tools(toolbox.endpoint)
            listing.add_row(
                f"[bold red]{UNGOVERNED}[/bold red]",
                "[red]none[/red]",
                f"[bold red]{', '.join(anon_names) or 'none'}[/bold red]",
            )
            result.record(UNGOVERNED, "tools/list", "deny",
                          reason="no identity, so no tools are published to it")
            narrate.show(listing)
            assert anon_names == [], "the endpoint published tools to an unauthenticated caller"

            # ------------------------------------------------------- promote
            sc = scenario("toolbox.supply_lookup")
            narrate.step(f"Supply Chain is asked: [white]{sc.prompt}[/white]")
            async with toolbox.session_for(principals["supply"]) as session:
                before = sorted(bare(f.name) for f in session.functions)
            narrate.detail(f"supply sees {before} - there is no supplier lookup at "
                           f"{toolbox.version}.")
            print()

            narrate.step("Publish toolbox [bold]v2[/bold] and promote it to default")
            promotion = toolbox.promote("v2")
            narrate.detail(
                f"default_version [bold]{promotion.previous}[/bold] -> "
                f"[bold green]{promotion.current}[/bold green]   "
                f"added: [green]{', '.join(promotion.added) or 'nothing'}[/green]"
            )
            narrate.detail("No agent was redeployed. No agent was restarted. "
                           "No agent code changed.")
            result.record("toolbox", "promote", promotion.current,
                          reason=f"added {', '.join(promotion.added)}")
            print()

            narrate.step("The same agent asks again")
            async with toolbox.session_for(principals["supply"]) as session:
                after = sorted(bare(f.name) for f in session.functions)
                narrate.detail(f"supply now sees {after}")
                for stp in sc.steps:
                    assert isinstance(stp, ToolCall)
                    fn = next((f for f in session.functions if bare(f.name) == stp.tool), None)
                    assert fn is not None, f"{stp.tool} did not appear after promotion"
                    narrate.detail(f"calls [bold]{stp.render()}[/bold]")
                    output = as_text(await fn.invoke(arguments=stp.arguments), limit=100)
                    output = output.splitlines()[0] if output else output
                    narrate.detail(f"   -> {output}")
                    result.record("supply", stp.tool, "available", reason="via toolbox v2")

            gained = sorted(set(after) - set(before))
            assert gained == ["lookup_supplier"], f"unexpected promotion result: {gained}"

            # trial_ops is unaffected by the promotion - it was never granted it.
            async with toolbox.session_for(principals["trial_ops"]) as session:
                trial_after = sorted(bare(f.name) for f in session.functions)
            narrate.detail(
                f"trial_ops still sees {trial_after} - a new tool in the toolbox is "
                "not a new grant."
            )
            result.record("trial_ops", "tools/list-after-promote", "filtered",
                          reason=",".join(trial_after))

    narrate.summary(
        [
            "Every agent now presents an identity it did not mint. "
            "Actions have a subject, so they can be attributed.",
            f"{UNGOVERNED} could not be issued one, so it never reached the "
            "endpoint at all. The shadow agent is now visible by its absence.",
            "One endpoint publishes the tools. The filtering is server-side - "
            "a filter the client applies to itself is a preference, not a control.",
            "Promoting v2 gave Supply Chain a new tool with no redeploy, and gave "
            "Clinical Operations nothing, because a tool in the toolbox is not a grant.",
            "But nothing has been stopped yet. safety_triage can still see "
            "update_case, and seeing it is still enough to call it.",
        ],
        closer="Identity and a single front door. Not yet a policy.",
        colour="cyan",
    )

    return result
