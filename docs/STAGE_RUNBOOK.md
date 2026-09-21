# Stage runbook

Everything you need on the day, in the order you need it.

---

## The evening before

```bash
uv run demo reset          # wipe the audit DB, reseed data, drop .env.chaos
docker compose up -d       # trace UI
uv run demo rehearse       # must print "Rehearsal passed"
```

If the rehearsal is not green, **do not go on stage until it is**. It runs all four acts and asserts every
decision the talk depends on; a green rehearsal is the only claim worth making about readiness.

Then open and leave open:

- <http://localhost:18888> — the trace UI (Aspire Dashboard)

**Do not** start the console yourself with `uv run demo console`. Acts 3 and 4
start their own on <http://localhost:8787>, and only that one can answer an
approval — the queue lives in the act's process. Open the browser tab once act 3
is running.

---

## Ten minutes before

```bash
uv run demo doctor         # imports, config, data, collector, trace UI
uv run demo reset          # start from a clean audit store
docker compose ps          # aspire container should be "Up"
```

Checklist:

- [ ] `doctor` reports no blocking problems
- [ ] Terminal font size raised; **at least 100 columns wide** or tables wrap
- [ ] Browser open on the console and the trace UI, zoomed so the back row can read them
- [ ] Notifications, Slack, Teams and email quit
- [ ] `DEMO_MODE` is what you intend — check the badge in the console masthead
- [ ] If presenting LIVE: `az login` done, `uv run python infra/start_live_tools.py` running in its own
      terminal (the Foundry toolbox calls tools through it), and `DEMO_MODE=live uv run demo doctor` is clean

---

## The sequence

Four commands, each under three minutes.

```bash
uv run demo act1
```
> Chaos. Ends on **"No identity. No policy. No trace."**
> Point at the damage table: a Clinical Ops agent edited a patient safety record, and an agent nobody
> registered raised a 500-unit purchase order.

```bash
uv run demo act2
```
> Identity and the toolbox. Two beats worth pausing on:
> - the `tools/list` table — each agent sees a different list, and `shadow_agent` sees nothing
> - the v1→v2 promotion — Supply Chain gains a tool, Clinical Ops gains nothing, nobody restarted

```bash
uv run demo act3
```
> Enforcement. **This is the act with the live interaction.**
> When the terminal says *"This one needs a human"*, switch to the console and click **Approve**.
> The act is blocked until you do, and denies itself after 60 seconds if you do not.
> Close on the partner table: the unknown vendor is up, healthy, answering — and received nothing.

```bash
uv run demo act4
```
> Visibility. It prints a trace id, then opens the console and the trace UI.
> Click the trace id in the console's feed. In the Aspire trace view, point out:
> - one trace, both agents, the delegation hop
> - `governance.policy update_case` sitting there for the whole escalation, then denied
> - `tools/call` nested under `execute_tool` — the trace crosses the MCP boundary

---

## Present the acts in MOCK

Not because LIVE is unreliable - it was verified working end to end - but because
**in LIVE the model chooses the tools, and it does not always choose what the
script expected.** Observed during verification: `safety_triage` called
`update_case`, then `read_case`, then `update_case` again; `shadow_agent` asked
for `lookup_supplier` instead of `create_po`; and in one act 4 run the model
never attempted the PHI write, so the escalation the act builds towards never
happened.

Every one of those was governed correctly, which is the strongest thing the talk
can say - the policy never consulted the planner. But an act whose closing beat
depends on a specific tool call should not be left to a model in front of an
audience.

Use LIVE to show the same layer with a real model, real Entra tokens and a real
Foundry toolbox. Use MOCK to land the beats.

```bash
DEMO_MODE=live uv run demo act2    # real toolbox, real promotion
DEMO_MODE=live uv run demo act3    # real model, policy still holding
```

See [LIVE_SETUP.md](LIVE_SETUP.md) - including the two preview behaviours that
will otherwise cost you an hour: Foundry namespaces tool names, and it caches
tool discovery per server label.

---

## Between rehearsals

```bash
uv run demo reset
```

Wipes `audit.db`, regenerates `data/*.json` from the fixed seed, and removes `.env.chaos`. Safe to run any
number of times; the committed fixtures are never modified by a run, because tool writes only mutate an
in-process overlay.

If a port is stuck (an act was killed mid-run):

```bash
# Windows
netstat -ano | findstr :8787
taskkill /PID <pid> /F
```

The tool servers and partner agents use ephemeral ports and clean themselves up; only the console's port
is fixed, and it falls back to another rather than failing the act - saying so when it does.

---

## If something goes wrong

### LIVE fails — model, Foundry, Entra or network

> *"We're switching to mock mode. Same governance code — only the identity provider, the tool source and
> the model client change. Everything you're about to see is the same policy, the same middleware and the
> same audit store."*

```bash
# Ctrl-C, then:
DEMO_MODE=mock uv run demo act3
```

This is true, not a save: `enforce.apply_decision` is the single seam every path goes through, and
`tests/test_middleware.py` proves both enforcement engines produce identical decisions.

### No trace id in act 4 / the trace UI is empty

```bash
docker compose up -d
uv run demo doctor          # "trace collector" should say reachable
```

Act 4 still runs and still shows every decision without a collector; only the trace links go nowhere.
Say so and move on — the audit table it prints has the same content.

### The Approve button does nothing

**First check you are on the console the act started.** The approval queue lives
in the act's own process, so a console you started separately with
`uv run demo console` has its own empty queue and its buttons do nothing for a
running act. Act 3 detects this and says so in red - but the fix is simply to
close the standalone console and use the URL the act printed.



The console is served by the act itself, so it only works **while the act is running**. If you approve
after the timeout the act has already denied, which is the correct behaviour and a fine thing to say out
loud. To retry: Ctrl-C and re-run `uv run demo act3`.

If the port was taken, the act warned you and printed a different URL — use the one in the terminal.
`uv run demo doctor` checks this before you start, and `CONSOLE_PORT` overrides it.

### An act hangs

Ctrl-C is handled: the servers shut down and the act exits. Re-run it. Nothing is left behind except the
audit rows, which `uv run demo reset` clears.

### Terminal tables look mangled

The terminal is too narrow or on a legacy code page. Widen to 100+ columns. The acts force UTF-8 output
themselves, so box characters should be correct.

---

## Timings from a clean machine

| Step | Time | Measured |
|---|---|---|
| `uv sync --all-extras --group dev` (cold clone) | ~26s | yes |
| `uv run demo rehearse` | ~14s | yes |
| `uv run demo act1` | ~2s | yes |
| `uv run demo act2` | ~5s | yes |
| `uv run demo act3` | ~6s + however long you take to click Approve | yes |
| `uv run demo act4` | ~13s (8s of which is the escalation expiring) | yes |
| `uv run pytest -q` | ~44s | yes |

Every act is comfortably inside three minutes; the constraint on stage is how
much you say over them, not how long they take. Act 3 is the only one that waits
for you.

Act 4's escalation timeout is `ACT4_APPROVAL_TIMEOUT` (default 8 seconds). Shorten it if you are tight;
lengthen it if you want to talk over the wait.

---

## The one-line version

```bash
uv run demo reset && uv run demo rehearse && uv run demo act1
```

If that is green, you are ready.
