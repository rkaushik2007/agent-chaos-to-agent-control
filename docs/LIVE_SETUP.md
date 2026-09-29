# LIVE setup

`DEMO_MODE=live` swaps three things and nothing else: the identity provider, the
tool source and the model client. The registry, the policy, the enforcement seam,
the A2A gateway, the audit store and the telemetry are the same code in both
modes — that is the claim the session makes, and it is why the runbook can say
"switching to mock mode, same governance code" and be telling the truth.

> **Preview.** Microsoft Foundry **Toolbox** is in public preview, and
> **Agent Hooks** (`GOVERNANCE_ENGINE=hooks`) uses an experimental SDK
> (`agent-hooks-sdk` 0.1.0a5). `agent-framework-a2a` and
> `agent-framework-foundry-hosting` are pre-release. Everything marked
> **(preview)** below can change.

## What exists in the verification project right now

These were created for the demo and are **billable while they exist** (Application
Insights is pay-per-GB with a free monthly grant; the rest are free or negligible).

| Resource | Name | Where |
|---|---|---|
| Log Analytics workspace | `helix-agent-governance-law` | `<your-resource-group>` |
| Application Insights | `helix-agent-governance-ai` | `<your-resource-group>` |
| Project connection (AppInsights) | `helix-agent-governance-ai` | the Foundry project |
| Toolbox | `helix-trial-tools`, versions 1 and 2, default **1** | the Foundry project |
| External agent registrations | `trial-ops`, `safety-triage`, `supply` (+ an Entra agent identity each) | the Foundry project |
| Dev tunnel | `helix-trial-tools` (expires 30 days after creation) | your devtunnel account |

Teardown is at the bottom of this page.

## What was actually verified, and what was not

Verified end to end against a real Foundry project on 2026-09-18:

| Thing | Status |
|---|---|
| `FoundryChatClient` against a real model deployment (`gpt-4o`) | ✅ verified |
| Entra tokens via `azure-identity` for `https://ai.azure.com/.default` | ✅ verified |
| Creating toolbox versions with `toolboxes.create_version` (preview) | ✅ verified |
| Promoting with `toolboxes.update(default_version=…)` (preview) | ✅ verified — the consumer endpoint went from 4 tools to 5 with nothing restarted |
| Consuming the toolbox over MCP with a bearer token | ✅ verified |
| Per-agent filtering against the live endpoint | ✅ verified (client-side — see below) |
| Act 2 and act 4 end to end in LIVE | ✅ verified |
| Traces in Application Insights | ✅ verified — **all 20 spans** of an act 4 trace, including every `governance.policy` span with `entra.agent_id`, `governance.decision`, `governance.rule_id` and `data.classification` |
| Registering the agents as Foundry **external agents** (preview) | ✅ verified — three registrations, no AI gateway, no endpoint, no charge |
| A per-agent **Entra agent identity**, provisioned by registering | ✅ verified — real object ids now stamped on every span and audit row |
| `governance.policy` spans attributable to a registered agent | ✅ verified — each carries `gen_ai.agent.id` as well as `entra.agent_id` |
| Foundry portal **Agents → _agent_ → Traces** | ⚠️ not verified by me — the spans and the registration both check out, the portal view is yours to open |
| **Acting *as* an Entra agent identity** | ❌ **still not possible** — the identity exists, the token exchange does not |
| `agent_framework_foundry_hosting.FoundryToolbox` | ⚠️ not exercised — this repo connects over MCP instead, for the reasons in `governance/toolbox_live.py` |

---

## Prerequisites

- An Azure subscription and a **Foundry project** with a deployed chat model.
- `az login`, with at least **Foundry User** on the project. The RBAC roles were
  renamed (Azure AI User → Foundry User); both names may appear in the portal.
- `uv sync --all-extras --group dev` — the `live` extra pulls
  `agent-framework-foundry`, `agent-framework-foundry-hosting`, `azure-identity`,
  `azure-ai-projects` and `azure-monitor-opentelemetry`.

> `azure-ai-projects` is pinned to **2.6.1**, not 2.7.0: `agent-framework-foundry`
> 1.13.1 requires `<2.7.0` while the Toolbox APIs need `>=2.3.0`.

Find your project endpoint:

```bash
az cognitiveservices account list -o table
az resource list --resource-type "Microsoft.CognitiveServices/accounts/projects" -o table
az cognitiveservices account deployment list -n <account> -g <rg> -o table
```

The endpoint is
`https://<account>.services.ai.azure.com/api/projects/<project>`.

---

## Environment

Copy `.env.example` to `.env` and fill in:

```bash
DEMO_MODE=live
FOUNDRY_PROJECT_ENDPOINT=https://<account>.services.ai.azure.com/api/projects/<project>
AZURE_AI_MODEL_DEPLOYMENT_NAME=<your-chat-deployment>
TOOLBOX_NAME=helix-clinical-tools
TOOLBOX_SERVER_LABEL_PREFIX=clinical_tools
REQUIRE_AGENT_IDENTITY=false      # see "Entra agent identities" below
```

Then:

```bash
DEMO_MODE=live uv run demo doctor
```

`.env` is git-ignored. Nothing in it is a secret — the endpoint and deployment
name are not credentials, and authentication is `az login`.

---

## Publishing `clinical-tools`

**A Foundry toolbox is a managed service and cannot reach loopback on your
laptop.** The `clinical-tools` MCP server has to be publicly addressable before
Foundry can discover its tools. An Azure Dev Tunnel is the quickest way.

```bash
winget install Microsoft.devtunnel
devtunnel user login -w -e          # integrated Windows auth, no browser prompt

# One server per catalogue version, because a toolbox version contains tool
# *sources* and Foundry reads the tool list from the server itself.
CLINICAL_TOOLS_PORT=8765 CLINICAL_TOOLS_VERSION=v1 uv run python -m mcp_servers.clinical_tools.server &
CLINICAL_TOOLS_PORT=8766 CLINICAL_TOOLS_VERSION=v2 uv run python -m mcp_servers.clinical_tools.server &

devtunnel create helix-clinical-tools --allow-anonymous
devtunnel port create helix-clinical-tools -p 8765 --protocol http
devtunnel port create helix-clinical-tools -p 8766 --protocol http
devtunnel host helix-clinical-tools
```

The tunnel prints a URL per port. The MCP endpoint is that URL plus `/mcp`.
Anonymous tunnels pass programmatic requests straight through — the
anti-phishing interstitial only applies to browsers.

> The data is synthetic, but a tunnel is a public inbound path to a process on
> your machine. Delete it when you are done: `devtunnel delete <name> -f`.

---

## Creating the toolbox (preview)

```bash
export CLINICAL_TOOLS_PUBLIC_URL_V1=https://<id>-8765.<region>.devtunnels.ms/mcp
export CLINICAL_TOOLS_PUBLIC_URL_V2=https://<id>-8766.<region>.devtunnels.ms/mcp

uv run python infra/create_toolbox.py --v1     # becomes default automatically
uv run python infra/create_toolbox.py          # v2 - published, NOT promoted
uv run python infra/create_toolbox.py --show
```

Record the version ids the script prints in `config/toolbox.yaml` under
`versions.<name>.live_version`, so act 2 can promote by catalogue name.

Then act 2's live beat:

```bash
uv run python infra/promote_toolbox.py 2
```

### Two endpoints

| | URL | Use |
|---|---|---|
| **Consumer** | `{project}/toolboxes/{name}/mcp?api-version=v1` | What agents connect to. Always serves `default_version`. |
| **Developer** | `{project}/toolboxes/{name}/versions/{n}/mcp?api-version=v1` | Pins one version, for testing before promoting. |

Bearer token scope: **`https://ai.azure.com/.default`**.

### Three preview behaviours that cost real debugging time

1. **Foundry namespaces every tool it re-publishes** as
   `<server_label>___<tool>` — `clinical_tools_v1___search_docs`. Policy here is
   written against the bare tool name, because it is the tool that is classified,
   not the route to it. `governance.enforce.guard_tool` strips the namespace
   before evaluating, and `governance.toolbox_live.qualify` adds it back when
   filtering. Nothing else in the layer knows about it.

2. **Tool discovery is cached per `server_label` within a project, and the cache
   survives deleting the toolbox.** A toolbox whose only version pointed at a
   server that demonstrably served four tools still published five, because that
   label had once seen five. A brand-new label against the same server published
   four immediately. So:
   - give each catalogue version its own `server_label`
     (`infra/create_toolbox.py` does — `{prefix}_{version}`), and
   - if a toolbox name has already seen a different tool set under a label, use a
     **new toolbox name**; recreating it under the same name kept serving the
     stale list.

3. **The SDK's method names differ from the Learn docs.** The docs say
   `list_toolbox_versions` / `get_toolbox_version` / `delete_toolbox_version`;
   `azure-ai-projects` 2.6.1 ships `list_versions` / `get_version` /
   `delete_version`. If the infra scripts break after an upgrade, check
   `dir(ToolboxesOperations)` first.

### Why this repo connects over MCP rather than `FoundryToolbox`

`agent_framework_foundry_hosting.FoundryToolbox` is the right choice for an agent
**hosted inside** Foundry. This demo runs agents locally and connects with
`MCPStreamableHTTPTool`, for two reasons:

- **Per-agent filtering.** `allowed_tools` gives each agent the filtered view the
  local toolbox enforces server-side. A Foundry toolbox does not currently expose
  a per-consumer tool ACL, so in LIVE **this filter is client-side and is not the
  authoritative control** — Foundry's authentication and guardrails are. Say so
  on stage. `# LIVE-TODO` in `governance/toolbox_live.py`.
- **Trace propagation.** Agent Framework injects W3C trace context into
  `tools/call` only for MCP sessions the agent process opens itself. Through a
  hosted connector the *service* issues that request, so act 4's end-to-end trace
  would stop at the toolbox boundary.

---

## Entra agent identities

**There is no client-side API to acquire a token whose subject is an Entra agent
identity.** Foundry provisions the blueprint and the identities, and Agent
Service performs the blueprint → agent identity → scoped token exchange at
tool-call time. The documentation is explicit that developers do not manage those
tokens.

So `EntraAgentIdentityProvider` does two things that are both real:

1. Carries the configured `agentIdentityId` as the governance claim, stamped on
   every span as `entra.agent_id` and on every audit row. That is the identifier
   an administrator applies Conditional Access and RBAC to.
2. Obtains a genuine Entra token for the resource being called, so the toolbox
   endpoint really does authenticate the caller — with the signed-in principal as
   its subject, not the agent identity.

**On stage, say "this is the agent's identity, and this is the token we present".
Do not say "this token was issued to the agent identity".**

### Where the ids come from

Registering the agents is what creates them. Foundry mints a
`ManagedAgentIdentityBlueprint` and an instance identity per external agent
registration, so each agent gets **its own** directory object rather than sharing
the project identity:

```bash
uv run python infra/register_agents.py          # creates the identities
uv run python infra/register_agents.py --env    # prints the lines below
```

```bash
TRIAL_OPS_AGENT_IDENTITY_ID=<object-id>-…      # trial-ops-<suffix>
SAFETY_TRIAGE_AGENT_IDENTITY_ID=<object-id>-…  # safety-triage-<suffix>
SUPPLY_AGENT_IDENTITY_ID=<object-id>-…         # supply-<suffix>
REQUIRE_AGENT_IDENTITY=true                 # refuse to start without them
```

Verified: with these set, an act 4 audit table shows `<object-id…` and
`<object-id…` where it used to show `unconfigured:trial_ops`, and every
`governance.policy` span in Application Insights carries the same id as
`entra.agent_id`.

This does **not** lift the limitation above. The identity is real and per-agent;
acquiring a token whose *subject* is that identity is still Agent Service's job.
What changed is that the id decisions are attributed to is now a directory object
an administrator can govern, instead of a label.

With `REQUIRE_AGENT_IDENTITY=false` the provider runs and labels the identity
`unconfigured:<agent>` — visibly not an Entra id, so a rehearsal trace can never
be mistaken for a real one.

Browse every agent identity in the tenant under **Entra ID → Agent ID → All agent
identities**.

---

## Telemetry

LIVE sends every span to **two places from one tracer provider**: the local
collector (Aspire) and the Foundry project's Application Insights. The connection
string is read from the project itself, so there is nothing to copy -
`APPLICATIONINSIGHTS_CONNECTION_STRING` overrides it. MOCK never looks it up.

### Two silent failures worth knowing about

Both cost real time, and both *looked* like success.

**1. No Application Insights at all.** `FoundryChatClient.configure_azure_monitor()`
does not raise when the project has none attached. It logs one line and installs
nothing. A "did it throw?" check reports success while exporting zero spans.

**2. Two providers.** The obvious sequence - configure OTLP, then call
`configure_azure_monitor()` - does not work. OpenTelemetry lets the global
provider be set once; the second call logs *"Overriding of current TracerProvider
is not allowed"* and is ignored. Its HTTP auto-instrumentation still ships a few
request spans to Azure, so **traces appear in Application Insights** - and it is
easy to stop looking there. But none of them are the ones that matter.

Measured on the same act 4 trace:

| | before | after |
|---|---|---|
| spans in Application Insights | 7 | 20 |
| `governance.policy` | 0 | 3 |
| `invoke_agent` / `chat` / `execute_tool` | 0 | 8 |

The fix is to pass the Azure Monitor exporters *into*
`configure_otel_providers(exporters=[...])`, so one provider feeds both
destinations - see `governance/telemetry.py`. The act now reports Azure export
only when an `AzureMonitorTraceExporter` is genuinely attached.

### Attaching Application Insights to a project

In the portal: **Foundry (new) → your project → Agents → Traces → Connect**, or
**Manage → Project details → Connected resources → Add connection → Application
Insights**. Or, as the verification did, by CLI:

```bash
az monitor log-analytics workspace create -g <rg> -n <name>-law -l <region>
az monitor app-insights component create -a <name>-ai -g <rg> -l <region> --workspace <law-resource-id>
```

then a `PUT` of an `AppInsights` connection to
`.../accounts/<account>/projects/<project>/connections/<name>?api-version=2025-06-01`
with `authType: ApiKey`, the component's resource id as `target` and
`metadata.ResourceId`, and its connection string as `credentials.key`.
On Git Bash set `MSYS_NO_PATHCONV=1` first, or resource ids are rewritten as
Windows paths.

### Seeing the traces

- **Application Insights** (Azure portal → `helix-agent-governance-ai`): verified.
  *Transaction search*, or *Logs* with
  ```kusto
  union dependencies, requests
  | where name startswith "governance.policy"
  | project timestamp, name,
      agent    = customDimensions["entra.agent_id"],
      decision = customDimensions["governance.decision"],
      rule     = customDimensions["governance.rule_id"]
  ```
  Allow two or three minutes for ingestion.
- **Foundry portal → Agents → Traces**: documented as the place Foundry shows
  traces, but **not verified here**. It is built around agents Foundry knows
  about, and these agents are not registered in Foundry - see *Registering the
  agents* below.

## Running LIVE

First, in its own terminal, start what the toolbox calls - and leave it running:

```bash
uv run python infra/start_live_tools.py      # or: make live-tools
```

It starts both catalogue servers and hosts the tunnel. The toolbox *object* is
visible in Foundry either way; only calling its tools needs this running.

Then, in another terminal:

```bash
DEMO_MODE=live uv run demo doctor
DEMO_MODE=live uv run demo act2     # real toolbox, real promotion
DEMO_MODE=live uv run demo act3     # real model deciding, policy enforcing
DEMO_MODE=live uv run demo act4     # real trace
```

### The model is not scripted, and that is the point

In MOCK a scripted planner emits fixed tool calls. In LIVE the model chooses, and
**it will not always choose what the script expected.** Observed in verification:

- `safety_triage` was asked to record a review and called `update_case`, then
  `read_case`, then `update_case` again with a different value. Every one of
  those was evaluated. Two were escalated and approved; the read was allowed.
- `shadow_agent` was asked to order supplies and called `lookup_supplier` and
  `search_docs` instead of `create_po`. All three were denied `REGISTRY-000`.
- In one act 4 run `safety_triage` chose `read_case` rather than the PHI write,
  so the escalation never happened and the act's closing beat did not land.

That is the strongest argument in the talk: the policy never consulted the
planner, so an unscripted model produced correctly governed outcomes anyway. It
is also why **the acts should be presented in MOCK**. Use LIVE to show the same
layer with a real model, not to hit exact beats on a timer.

---

## Cost

Small. The only metered items are model tokens and Application Insights ingest.

| | |
|---|---|
| A full LIVE act | a few thousand tokens — cents on a `gpt-4o`-class deployment |
| Toolbox (preview) | no separate charge observed; the tools behind it may bill |
| Application Insights | a few MB per run, within most free grants |
| External agent registration | free — metadata only. The *other* path, control-plane custom agents, needs Azure API Management and is not free; this repo does not use it. |
| Dev tunnel | free |
| Aspire Dashboard | local container, free |

The expensive mistake is leaving a model deployment provisioned at high capacity.
Check with `az cognitiveservices account deployment list -n <account> -g <rg>`.

---

## Registering the agents in Foundry

```bash
uv run python infra/register_agents.py           # register all three
uv run python infra/register_agents.py --show    # names, otel ids, Entra identities
uv run python infra/register_agents.py --env     # the .env lines to paste
uv run python infra/register_agents.py --delete  # remove them
```

`trial_ops`, `safety_triage` and `supply` are Agent Framework objects inside the
act's process, so by default Foundry has a toolbox to show and nothing that looks
like an agent. Registering them as **external agents** closes that gap for
nothing: one metadata record per agent saying "this agent exists and emits
telemetry under this id". Foundry matches spans in the project's Application
Insights by `gen_ai.agent.id` and shows them under **Agents → _agent_ →
Traces**. Foundry does not host, proxy or invoke anything.

The only prerequisite that matters is Application Insights connected to the
project, which act 4 already needs.

Preview: create and update requests need the
`Foundry-Features: ExternalAgents=V1Preview` header, which the Python SDK sends
when `AIProjectClient` is built with `allow_preview=True`.

`shadow_agent` is not registered, deliberately. The registry is the only source
of an agent's rights and Foundry's list should agree: an agent nobody owns is an
agent nobody can see.

### Two things the docs get wrong

| | |
|---|---|
| **Name characters** | The docs allow "alphanumeric characters, hyphens, and underscores". The service rejects `safety_triage` with *"Must start and end with alphanumeric characters, can contain hyphens in the middle"*. So the Foundry names are `trial-ops`, `safety-triage` and `supply`, while `otel_agent_id` carries the underscored id the spans actually use. `infra/register_agents.py` derives both from the registry key. |
| **Entra identities** | Not mentioned on the external-agent page. Registering an agent makes Foundry mint a `ManagedAgentIdentityBlueprint` and an instance identity for it — a real per-agent directory object, even though Foundry never runs the agent. See below. |

### Why not *Operate → Register asset*

That is the other path — Foundry **control plane** custom agents — and it is a
different thing. It gives a block/unblock switch and a trace per HTTP call, but
it needs an **AI gateway (Azure API Management)** in front, one exclusive
reachable endpoint per agent, and it issues a new client URL that callers must
use instead of the original. It governs the *transport*.

This repo's argument is that governance belongs at the tool and delegation
boundary, which is where the middleware sits — so the external-agent path is the
honest fit, and it costs nothing. If you want the gateway story too, the docs are
at [register-custom-agent](https://learn.microsoft.com/en-us/azure/foundry/control-plane/register-custom-agent).

## Teardown

Everything created for the demo, most reversible first:

```bash
# the agent registrations (the agents keep running; their Entra identities go)
uv run python infra/register_agents.py --delete

# the toolbox (its endpoint stops working immediately)
uv run python -c "import os;from azure.ai.projects import AIProjectClient;from azure.identity import AzureCliCredential;from governance import settings;settings.load_env();c=AIProjectClient(endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'],credential=AzureCliCredential());c.toolboxes.delete(name=os.environ['TOOLBOX_NAME']);print('deleted')"

# the tunnel
devtunnel delete helix-trial-tools -f

# Application Insights and its workspace (this deletes the stored traces)
az monitor app-insights component delete -a helix-agent-governance-ai -g <your-resource-group>
az monitor log-analytics workspace delete -n helix-agent-governance-law -g <your-resource-group> --yes

docker compose down
```

Remove the `AppInsights` connection from the project too (Manage → Project
details → Connected resources), or it will point at a deleted resource.
Clear `TOOLBOX_ENDPOINT` from `.env` afterwards.
