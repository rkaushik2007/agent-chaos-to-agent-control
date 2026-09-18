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
- <http://localhost:8000> — the governance console (acts 3 and 4 start it themselves)

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
- [ ] If presenting LIVE: `az login` done, and `DEMO_MODE=live uv run demo doctor` is clean

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
netstat -ano | findstr :8000
taskkill /PID <pid> /F
```

The tool servers and partner agents use ephemeral ports and clean themselves up; only the console's 8000
is fixed, and it falls back to another port rather than failing the act.

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

The console is served by the act itself, so it only works **while the act is running**. If you approve
after the timeout the act has already denied, which is the correct behaviour and a fine thing to say out
loud. To retry: Ctrl-C and re-run `uv run demo act3`.

If port 8000 was taken, the act printed a different URL — use the one in the terminal.

### An act hangs

Ctrl-C is handled: the servers shut down and the act exits. Re-run it. Nothing is left behind except the
audit rows, which `uv run demo reset` clears.

### Terminal tables look mangled

The terminal is too narrow or on a legacy code page. Widen to 100+ columns. The acts force UTF-8 output
themselves, so box characters should be correct.

---

## Timings from a clean machine

| Step | Time |
|---|---|
| `uv sync --all-extras --group dev` (cold) | ~60s |
| `uv run demo rehearse` | ~25s |
| `uv run demo act1` | ~8s |
| `uv run demo act2` | ~8s |
| `uv run demo act3` | ~20s + however long you take to click Approve |
| `uv run demo act4` | ~15s (8s of which is the escalation expiring) |
| `uv run pytest -q` | ~60s |

Act 4's escalation timeout is `ACT4_APPROVAL_TIMEOUT` (default 8 seconds). Shorten it if you are tight;
lengthen it if you want to talk over the wait.

---

## The one-line version

```bash
uv run demo reset && uv run demo rehearse && uv run demo act1
```

If that is green, you are ready.
