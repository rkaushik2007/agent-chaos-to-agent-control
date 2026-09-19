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
| Azure Monitor export from the project | ✅ verified (`configure_azure_monitor` reported success) |
| **Acting *as* an Entra agent identity** | ❌ **not possible today** — see below |
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

Find the ids in the portal: the Foundry project's **Overview → JSON View** for
the shared project identity, or a published agent application's JSON View for a
distinct one. Then:

```bash
TRIAL_OPS_AGENT_IDENTITY_ID=<agentIdentityId>
SAFETY_TRIAGE_AGENT_IDENTITY_ID=<agentIdentityId>
SUPPLY_AGENT_IDENTITY_ID=<agentIdentityId>
REQUIRE_AGENT_IDENTITY=true     # refuse to start without them
```

With `REQUIRE_AGENT_IDENTITY=false` the provider runs and labels the identity
`unconfigured:<agent>` — visibly not an Entra id, so a rehearsal trace can never
be mistaken for a real one.

Browse every agent identity in the tenant under **Entra ID → Agent ID → All agent
identities**.

---

## Telemetry

Local collector as usual (`docker compose up -d`). In LIVE the acts additionally
call `FoundryChatClient.configure_azure_monitor()`, which reads the connection
string from the project — nothing to copy. Set
`APPLICATIONINSIGHTS_CONNECTION_STRING` to override. If the project has no
Application Insights attached, the act says so and carries on with the local
collector only.

---

## Running LIVE

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
| Dev tunnel | free |
| Aspire Dashboard | local container, free |

The expensive mistake is leaving a model deployment provisioned at high capacity.
Check with `az cognitiveservices account deployment list -n <account> -g <rg>`.

---

## Teardown

```bash
uv run python -c "import os;from azure.ai.projects import AIProjectClient;from azure.identity import AzureCliCredential;from governance import settings;settings.load_env();c=AIProjectClient(endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'],credential=AzureCliCredential());c.toolboxes.delete(name=os.environ['TOOLBOX_NAME']);print('deleted')"
devtunnel delete helix-clinical-tools -f
docker compose down
```

Deleting the toolbox breaks its endpoint immediately. Clear `TOOLBOX_ENDPOINT`
from `.env` afterwards.
