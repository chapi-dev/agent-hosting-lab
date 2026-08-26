# Runbook 1 — Deploy a Foundry Hosted Agent

From nothing to a working hosted agent. Every command here was run for real, and the
"How the hosted contract works" section explains the behaviour behind each requirement.

**Time:** ~20 minutes, most of it waiting for the first deploy.

---

## Prerequisites

```powershell
az --version                    # Azure CLI, logged in: az login
azd version                     # >= 1.31
python --version                # 3.11+
```

Install the Foundry extension for `azd`:

```powershell
azd ext install microsoft.foundry
azd ai agent version
```

You need a Foundry account with a project and at least one model deployment. If you do not have
one, `infra/main.bicep` in this repo creates the whole platform:

```powershell
az group create -n rg-agenthost-lab -l swedencentral
az deployment group create -g rg-agenthost-lab -f infra/main.bicep -p prefix=aghl
```

Note the outputs — you need the **project endpoint** and the **model deployment name**.

---

## Step 1 — Write the agent

The hosted contract is small. Everything below is the complete deployable unit.

`src/hosted/main.py`:

```python
import inspect, os
from azure.ai.agentserver.responses import ResponsesAgentServerHost, TextResponse
from agent_core import DiskStore, build_agent, build_chat_client

STORE = DiskStore(os.environ.get("STATE_DIR") or os.path.expanduser("~/trip"))
app = ResponsesAgentServerHost()
_client = build_chat_client()

@app.response_handler
async def handle(request, context, cancellation_signal):
    agent = build_agent(STORE, "session", client=_client)
    result = await agent.run(await _input_text(context))
    return TextResponse(context, request, text=result.text.strip())

async def _input_text(context) -> str:
    value = context.get_input_text()
    if inspect.isawaitable(value):
        value = await value
    return value or ""

if __name__ == "__main__":
    app.run()
```

`src/hosted/requirements.txt`:

```
agent-framework-core
agent-framework-foundry
azure-ai-agentserver-responses
azure-identity
```

> **Use `agent-framework-core`, not `agent-framework`.** The metapackage cannot resolve in the
> hosted build environment — see [Dependencies resolve in Azure](#dependencies-resolve-in-azure-not-on-your-machine).

Anything the agent imports must sit next to `main.py`. This lab keeps the shared agent in
`src/agent_core/` and copies it in before deploying — see `scripts/prepare-hosted.ps1`.

## Step 2 — Initialise the azd project

```powershell
azd ai agent init `
  --src ./src/hosted `
  --deploy-mode code `
  --dep-resolution remote_build
```

- `--deploy-mode code` ships source, not a container. No Dockerfile.
- `--dep-resolution remote_build` resolves dependencies **in Azure**. No local Docker required.

This writes `azure.yaml`.

## Step 3 — Declare your own configuration

`init` writes a skeleton. Open `azure.yaml` and add the settings the platform cannot know about
— your model deployment name and the container size:

```yaml
services:
    trip-planner:
        project: ./src/hosted
        host: azure.ai.agent
        language: python
        uses:
            - <your-project-service>
        codeConfiguration:
            dependencyResolution: remote_build
            entryPoint: main.py
            runtime: python_3_13
        env:
            MODEL_DEPLOYMENT_NAME: gpt-5.4-mini
        container:
            resources:
                cpu: "0.5"
                memory: 1Gi
        kind: hosted
        protocols:
            - protocol: responses
              version: 2.0.0
```

Two notes on this block:

- **Do not add the project endpoint here.** The platform injects it as
  `FOUNDRY_PROJECT_ENDPOINT`; read that name from your code. See
  [What the platform gives your container](#what-the-platform-gives-your-container).
- **`container.resources` accepts `cpu` from `"0.25"` to `"4.0"` and `memory` from `0.5Gi` to
  `8.0Gi`.** It is optional, but an unstated default is a default that can change under you.
  If your agent needs more than 4 vCPU or 8 GiB, hosted is not the right model for it.

## Step 4 — Deploy

```powershell
azd deploy trip-planner
```

First deploy takes 5–10 minutes (remote dependency resolution and image build). Subsequent
deploys are faster.

## Step 5 — Verify

```powershell
azd ai agent show trip-planner
```

```
Status                active
Version               7
Endpoint (responses)  https://<account>.services.ai.azure.com/api/projects/<project>/agents/trip-planner/endpoint/protocols/openai/responses?api-version=v1
```

Send a message:

```powershell
azd ai agent invoke trip-planner "Add Paris to my trip"
```

Multi-turn — `invoke` carries the session for you:

```powershell
azd ai agent invoke trip-planner "Add Rome"
azd ai agent invoke trip-planner "What is in my trip?"
# -> Your trip is: Paris -> Rome.
```

To see the raw HTTP exchange, including session headers:

```powershell
azd ai agent invoke trip-planner "hello" -o raw
```

Logs, when something is wrong:

```powershell
azd ai agent monitor trip-planner --type console --session-id <session-id>
```

`--type` accepts only `console` or `system`, and a session id is required. Get one from an
`invoke -o raw`, or from `azd ai agent sessions list trip-planner`.

## Step 6 — Call it from your own code

```python
import httpx
from azure.identity import DefaultAzureCredential

ENDPOINT = "https://.../agents/trip-planner/endpoint/protocols/openai/responses?api-version=v1"
token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Turn 1 - optionally attach a pre-created session (see Step 7)
r1 = httpx.post(ENDPOINT, headers=headers, json={"input": "Add Paris", "store": True})
b1 = r1.json()
session_id = b1.get("agent_session_id") or r1.headers.get("x-agent-session-id")

# Turn 2 - BOTH handles, BOTH in the body
r2 = httpx.post(ENDPOINT, headers=headers, json={
    "input": "What is in my trip?",
    "store": True,
    "agent_session_id": session_id,          # selects the sandbox
    "previous_response_id": b1["id"],        # continues the conversation
})
```

> `agent_session_id` goes in the **body**. The `x-agent-session-id` header is only honoured
> alongside `previous_response_id` and is ignored on turn one — see
> [How sessions carry state](#how-sessions-carry-state).

## Step 7 — Pre-create sessions (do this in production)

Sandbox provisioning costs ~6.5 s. Start it when the UI opens, not when the user sends.

```powershell
azd ai agent sessions create trip-planner
```

```json
{ "agent_session_id": "...", "status": "active", "expires_at": 1790306508 }
```

Over REST:

```http
POST https://<account>.services.ai.azure.com/api/projects/<project>/agents/trip-planner/endpoint/sessions?api-version=v1
{}
```

Note the URL: sessions hang off the **agent endpoint root**, not the protocol path.
`.../endpoint/sessions` works; `.../endpoint/protocols/openai/sessions` returns 404.

Measured effect: first real turn drops from **9458 ms → 3174 ms**. Sessions expire after 30 days.

Other session commands:

```powershell
azd ai agent sessions list trip-planner
azd ai agent sessions show trip-planner <session-id>
azd ai agent sessions stop trip-planner <session-id>
azd ai agent sessions delete trip-planner <session-id>
```

## Step 8 — CI/CD

```yaml
- name: Deploy agent
  run: |
    azd ext install microsoft.foundry
    ./scripts/prepare-hosted.ps1
    azd deploy trip-planner --no-prompt
  env:
    AZURE_CLIENT_ID: ${{ secrets.AZURE_CLIENT_ID }}
    AZURE_TENANT_ID: ${{ secrets.AZURE_TENANT_ID }}
    AZURE_SUBSCRIPTION_ID: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
```

Use federated credentials (OIDC), not a client secret.

**Versions are immutable and there is no traffic splitting.** Every deploy creates a new version
and it becomes current immediately. If you need gradual rollout, deploy a second named agent and
shift traffic in your router — which is one more reason to have a router.

---

## How the hosted contract works

The hosted runtime asks little of your code, but what it asks is exact. This section explains
each part of the contract, what the platform does on your behalf, and how a mismatch shows up
when you get one wrong.

### Dependencies resolve in Azure, not on your machine

With `dependency_resolution: remote_build`, your `requirements.txt` is resolved inside the
hosted build environment. That environment is not your laptop: a dependency tree that installs
locally can still fail there.

The `agent-framework` metapackage is the common case. It pulls in `agent-framework-hyperlight`,
which cannot resolve during a remote build:

```
ERROR: Cannot install agent-framework ... ResolutionImpossible
(13 versions of agent-framework-hyperlight)
```

Depend on the core package instead, and add the integrations you actually use:

```diff
- agent-framework
+ agent-framework-core
```

The practical rule: keep hosted dependencies narrow. Every extra package is another chance for
the remote resolver to disagree with your machine.

### What the platform gives your container

The platform injects a substantial environment under the reserved `FOUNDRY_` and `AGENT_`
prefixes. This lab asked the deployed agent to report its own environment (the `describe_runtime`
tool in `src/agent_core/agent.py`), so the following is observed from version 9, not documented
from memory:

```
FOUNDRY_PROJECT_ENDPOINT          FOUNDRY_AGENT_ID
FOUNDRY_PROJECT_ARM_ID            FOUNDRY_AGENT_NAME
FOUNDRY_HOSTING_ENVIRONMENT       FOUNDRY_AGENT_VERSION
FOUNDRY_AGENT_SESSION_ID          FOUNDRY_AGENT_TENANT_ID
FOUNDRY_AGENT_TOOLSET_ENDPOINT    FOUNDRY_AGENT_TOOLSET_FEATURES
FOUNDRY_AGENT_INSTANCE_CLIENT_ID  FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID
FOUNDRY_AGENT_DEFAULT_INSTANCE_CLIENT_ID
FOUNDRY_AGENT365_TRACING_ENABLED

APPLICATIONINSIGHTS_CONNECTION_STRING   IDENTITY_ENDPOINT   HOME=/home/session
```

Three things worth reading off that list:

- **The project endpoint is injected.** Read `FOUNDRY_PROJECT_ENDPOINT` rather than declaring
  your own copy. The reference documentation is explicit that redeclaring it in `env:` is
  redundant and risks shadowing the platform value.
- **The agent knows its own session.** `FOUNDRY_AGENT_SESSION_ID` identifies the sandbox serving
  the current request, which is why keying state by a constant is correct (see
  [How sessions carry state](#how-sessions-carry-state)).
- **Identity arrives without configuration.** `IDENTITY_ENDPOINT` is present while
  `AZURE_CLIENT_ID` and `MSI_ENDPOINT` are absent, so `DefaultAzureCredential()` works with no
  secrets and nothing to configure.

If your code reads a name the platform does not set, it raises `KeyError` at import, the session
never reaches readiness, and the caller sees an HTTP 424 `session_not_ready` that says nothing
about environment variables. That failure means *you are reading the wrong name*, not that the
value is missing — check the reserved prefixes before adding your own variable.

> This lab learned that the slow way. Earlier versions of this runbook concluded "telemetry is
> injected, configuration is yours" after a 424 on `KeyError: 'PROJECT_ENDPOINT'`. The endpoint
> was there all along under `FOUNDRY_PROJECT_ENDPOINT`. The `env:` block in Step 3 keeps working
> because `AZURE_AI_PROJECT_ENDPOINT` is not a reserved name, but it is a copy you do not need.

### The response handler signature

The runtime calls your handler with exactly three positional parameters, in this order:

```python
async def handle(request, context, cancellation_signal):
```

Anything else is rejected as the module is imported, so it also surfaces as a 424 rather than as
a clear error at call time:

```
TypeError: response_handler must take exactly three positional parameters
(request, context, cancellation_signal)
```

### The runtime is asynchronous end to end

`context.get_input_text()` returns a coroutine in the deployed runtime, while some local builds
return a plain value. Passing an un-awaited coroutine onward fails deep inside telemetry, which
points at framework internals rather than at your line:

```
TypeError: 'coroutine' object is not iterable
```

Awaiting conditionally keeps local and deployed on the same path:

```python
value = context.get_input_text()
if inspect.isawaitable(value):
    value = await value
```

### The runtime streams what you return

The runtime iterates over your return value to stream it, so it expects a response object rather
than a string. Returning a bare `str` produces:

```
TypeError: 'async for' requires an object with __aiter__ method, got str
```

Wrap the text:

```python
return TextResponse(context, request, text=result.text.strip())
```

### How sessions carry state

Two behaviours decide whether multi-turn conversations work, and both fail silently — the
request succeeds and the answer is confidently wrong.

**Key state by a constant, not by a conversation id.** `context.conversation_chain_id` is not
stable across turns, so every turn reads and writes a different key:

```python
agent = build_agent(STORE, context.conversation_chain_id)   # changes every turn
agent = build_agent(STORE, "session")                       # correct
```

A constant looks careless and is right: the sandbox is already private to the session — the
runtime even tells the agent which one it is via `FOUNDRY_AGENT_SESSION_ID` — so there is
nothing left to key by.

**Attach the session in the body, not the header.** Measured over four rounds against
pre-created sessions:

| Mechanism | Reattached | First turn |
|---|---|---|
| body `agent_session_id` | 4/4 | 3174 ms |
| header `x-agent-session-id` | 0/4 | 9458 ms |

The header is only honoured alongside `previous_response_id`, which by definition does not exist
on turn one — the exact turn pre-warming targets. Put the id in the body.

---

## Checklist

- [ ] `agent-framework-core`, not `agent-framework`
- [ ] Project endpoint read from `FOUNDRY_PROJECT_ENDPOINT`, not redeclared under a reserved name
- [ ] `MODEL_DEPLOYMENT_NAME` declared in `azure.yaml`
- [ ] `container.resources` set explicitly (`cpu` "0.25"–"4.0", `memory` 0.5Gi–8.0Gi)
- [ ] Handler signature is `(request, context, cancellation_signal)`
- [ ] `get_input_text()` awaited conditionally
- [ ] Handler returns `TextResponse`, not `str`
- [ ] State keyed by a constant
- [ ] Everything the agent imports is inside the deployed directory
- [ ] Clients send `agent_session_id` in the **body**
- [ ] Sessions pre-created when the UI opens
- [ ] CI uses OIDC federated credentials
