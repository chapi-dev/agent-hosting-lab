# Runbook 1 — Deploy a Foundry Hosted Agent

From nothing to a working hosted agent. Every command here was run for real; every error in the
"traps" section actually happened while building this lab.

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

> **Use `agent-framework-core`, not `agent-framework`.** See Trap 1.

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

## Step 3 — Add the configuration the platform does not inject

**This step is not optional and `init` does not do it for you.** Open `azure.yaml` and add an
`env:` block to the agent service:

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
            AZURE_AI_PROJECT_ENDPOINT: https://<account>.services.ai.azure.com/api/projects/<project>
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

`APPLICATIONINSIGHTS_CONNECTION_STRING` **is** injected. Your project endpoint is **not**. See
Trap 2 — this is the single most expensive assumption in this runbook.

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
> alongside `previous_response_id` and is ignored on turn one. See Trap 6.

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

## The six traps

Every one of these cost real time while building this lab. They are listed in the order they
appear.

### Trap 1 — `ResolutionImpossible` during dependency resolution

```
ERROR: Cannot install agent-framework ... ResolutionImpossible
(13 versions of agent-framework-hyperlight)
```

**Cause:** the `agent-framework` metapackage pulls in `agent-framework-hyperlight`, which cannot
resolve in the hosted build environment.

**Fix:** depend on `agent-framework-core` instead.

```diff
- agent-framework
+ agent-framework-core
```

### Trap 2 — HTTP 424 `session_not_ready` with `KeyError: 'PROJECT_ENDPOINT'`

**Cause:** the platform injects `APPLICATIONINSIGHTS_CONNECTION_STRING`, so it is natural to
assume it injects the project endpoint too. It does not. The agent crashes at import, the
session never reaches readiness, and the caller sees a 424 that says nothing about environment
variables.

**Fix:** declare it in `azure.yaml` under `env:` (Step 3).

**Rule of thumb:** telemetry is injected, configuration is yours.

### Trap 3 — `response_handler must take exactly three positional parameters`

```
TypeError: response_handler must take exactly three positional parameters
(request, context, cancellation_signal)
```

**Cause:** wrong handler signature. This fails at import time, so it also surfaces as a 424.

**Fix:**

```python
async def handle(request, context, cancellation_signal):
```

Exactly those three, in exactly that order.

### Trap 4 — `'coroutine' object is not iterable`

**Cause:** `context.get_input_text()` is a coroutine in the deployed runtime and a plain method
in some local builds. Passing the un-awaited coroutine to the agent fails deep inside telemetry,
pointing at framework internals rather than at your line.

**Fix:** await conditionally, so local and deployed take the same path.

```python
value = context.get_input_text()
if inspect.isawaitable(value):
    value = await value
```

### Trap 5 — `'async for' requires an object with __aiter__ method, got str`

**Cause:** the handler returned a bare string. The runtime streams whatever it gets back.

**Fix:**

```python
return TextResponse(context, request, text=result.text.strip())
```

### Trap 6 — state disappears between turns

Two distinct causes, both silent, both producing confident wrong answers.

**6a. Keying state by `context.conversation_chain_id`.** It is not stable across turns, so every
turn reads and writes a different key.

```python
agent = build_agent(STORE, context.conversation_chain_id)   # WRONG
agent = build_agent(STORE, "session")                       # correct
```

A constant looks careless and is right: the sandbox is already private per session, so there is
nothing left to key by.

**6b. Attaching the session with the header instead of the body field.** Measured over 4 rounds
against pre-created sessions:

| Mechanism | Reattached | First turn |
|---|---|---|
| body `agent_session_id` | 4/4 | 3174 ms |
| header `x-agent-session-id` | 0/4 | 9458 ms |

The header is only honoured alongside `previous_response_id`. Put the id in the body.

---

## Checklist

- [ ] `agent-framework-core`, not `agent-framework`
- [ ] `AZURE_AI_PROJECT_ENDPOINT` and `MODEL_DEPLOYMENT_NAME` declared in `azure.yaml`
- [ ] Handler signature is `(request, context, cancellation_signal)`
- [ ] `get_input_text()` awaited conditionally
- [ ] Handler returns `TextResponse`, not `str`
- [ ] State keyed by a constant
- [ ] Everything the agent imports is inside the deployed directory
- [ ] Clients send `agent_session_id` in the **body**
- [ ] Sessions pre-created when the UI opens
- [ ] CI uses OIDC federated credentials
