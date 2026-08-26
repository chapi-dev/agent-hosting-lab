# Runbook 4 — Troubleshooting

Every error this lab actually hit, with its real cause and fix. Ordered by where you are likely
to meet it.

---

## Hosted agent

### `ResolutionImpossible` mentioning `agent-framework-hyperlight`

```
ERROR: Cannot install agent-framework ... ResolutionImpossible
(13 versions of agent-framework-hyperlight)
```

The `agent-framework` metapackage pulls `agent-framework-hyperlight`, which cannot resolve in
the hosted build environment.

```diff
- agent-framework
+ agent-framework-core
```

Related: `agent-framework 1.15.0` is not installable at all — it requires
`agent-framework-core==1.15.0`, which is not on PyPI.

### HTTP 424 `session_not_ready`

The agent crashed during startup, so the sandbox never reached readiness. The 424 tells you
nothing about why. Get the real error:

```powershell
azd ai agent sessions list trip-planner
azd ai agent monitor trip-planner --type console --session-id <session-id>
```

The three causes seen in this lab, all import-time crashes:

| Log line | Cause | Fix |
|---|---|---|
| `KeyError: 'PROJECT_ENDPOINT'` | env var not injected | declare it in `azure.yaml` under `env:` |
| `response_handler must take exactly three positional parameters` | wrong signature | `async def handle(request, context, cancellation_signal)` |
| `ModuleNotFoundError` | dependency missing from the deployed directory | everything imported must live next to `main.py` |

### `KeyError: 'PROJECT_ENDPOINT'` specifically

The platform injects `APPLICATIONINSIGHTS_CONNECTION_STRING`. It does **not** inject your
project endpoint. Add to `azure.yaml`:

```yaml
        env:
            AZURE_AI_PROJECT_ENDPOINT: https://<account>.services.ai.azure.com/api/projects/<project>
            MODEL_DEPLOYMENT_NAME: <deployment>
```

**Telemetry is injected. Configuration is yours.**

### `TypeError: 'coroutine' object is not iterable`

`context.get_input_text()` is a coroutine in the deployed runtime, a plain method in some local
builds. The traceback points into `agent_framework` telemetry, not at your code.

```python
value = context.get_input_text()
if inspect.isawaitable(value):
    value = await value
```

### `'async for' requires an object with __aiter__ method, got str`

The handler returned a bare string; the runtime streams the return value.

```python
return TextResponse(context, request, text=result.text.strip())
```

### The agent forgets everything between turns

Three distinct causes. Work through them in order.

**1. State keyed by something unstable.**

```python
build_agent(STORE, context.conversation_chain_id)   # not stable across turns
build_agent(STORE, "session")                       # correct
```

**2. The client is not sending `agent_session_id`.** Without it, every turn gets a fresh sandbox.

**3. The client is sending it in the header.** `x-agent-session-id` is only honoured alongside
`previous_response_id`, so it is ignored on turn one:

| Mechanism | Reattached | First turn |
|---|---|---|
| body `agent_session_id` | 4/4 | 3174 ms |
| header `x-agent-session-id` | 0/4 | 9458 ms |

Put it in the body.

### Every turn takes ~9 s, not just the first

Same root cause as (2) and (3) above, but with no visible error to lead you there — the answers
are correct, they are just uniformly slow. The tell is the *shape* of the latency curve:

| | Turn 1 | Turn 2 | Turn 3 |
|---|---|---|---|
| Healthy | 10004 ms | 2413 ms | 2413 ms |
| Session dropped | 9115 ms | 9634 ms | 9634 ms |

A healthy session pays the sandbox cold start once. If turn 2 costs the same as turn 1, you are
opening a new sandbox every turn: check that the client stores `agent_session_id` and
`previous_response_id` from each response and sends both back on the next request.

```powershell
# Confirm the id is stable across turns - it should be identical every time
curl -s -X POST "$env:ROUTER_URL/chat" -H "content-type: application/json" `
  -d '{"session_id":"probe","message":"hello"}' | ConvertFrom-Json |
  Select-Object agent_session_id, previous_response_id
```

Over a twenty-turn conversation this is the difference between 56 s and 193 s of cumulative
latency. See `time_router()` in `experiments/02_cold_start.py` for a correct client loop.

### Diagnosing generally

```powershell
azd ai agent doctor
azd ai agent show trip-planner
azd ai agent invoke trip-planner "hello" -o raw     # dumps headers
```

---

## Self-hosted

### Cosmos: `Request originated from IP ... blocked by your firewall settings`

**Check for a `modify` policy before touching anything.**

```powershell
$cosmos = az cosmosdb list -g <rg> --query "[0].id" -o tsv
az policy state list --resource $cosmos `
  --query "[?policyDefinitionAction=='modify'].{policy:policyDefinitionName, assign:policyAssignmentName}" -o table
```

If `CosmosDB_PublicNetwork_Modify` appears, **stop trying to enable public access.** Bicep,
`az cosmosdb update` and `az rest PATCH` will all report success and be reverted within seconds.
Nothing errors, which is why this wastes so much time.

Use a private endpoint. See [runbook 2](02-deploy-self-hosted-agent.md).

### Cosmos: `Unauthorized` even with the right identity

`CosmosDB_LocalAuth_Modify` disables key auth, and ARM RBAC is not the same as Cosmos data-plane
RBAC. You need an explicit SQL role assignment:

```powershell
az cosmosdb sql role assignment list -g <rg> -a <account> -o table
```

If the identity is missing, assign built-in Data Contributor (`...sqlRoleDefinitions/00000000-0000-0000-0000-000000000002`).

### `ContainerAppEnvironmentMismatch`

A Container Apps environment's VNet is immutable, and apps cannot move between environments.
Delete and recreate them:

```powershell
az containerapp delete -g <rg> -n <app> --yes
az deployment group create -g <rg> -f infra/apps.bicep -p environmentName=<new-env> ...
```

Decide on VNet integration **before** the first deploy.

### `az acr build` fails with `UnicodeEncodeError`

Windows console encoding (cp1252) cannot render the build log stream. **The build usually
succeeds anyway.**

```powershell
az acr build --registry <acr> --image <img> --file src/Dockerfile ./src --no-logs
az acr task logs -r <acr> --run-id <id>          # read them separately
```

### `COPY failed: file not found in build context`

The build context is wrong. `COPY selfhosted/requirements.txt` is relative to the context, so
the context must be `./src`, not the repository root:

```powershell
az acr build ... --file src/Dockerfile ./src
```

### The agent loses state across turns

Confirm it is the replica problem before looking anywhere else — check the `replica` field in
each response. Different replicas plus `STATE_BACKEND=disk` is the bug.

```powershell
$r = Invoke-RestMethod -Method Post "$u/chat" -ContentType application/json -Body $b
$r.replica; $r.state_backend
```

Fix: `STATE_BACKEND=cosmos`, or any shared store.

### Container Apps: revision stuck in `Provisioning`

```powershell
az containerapp revision list -g <rg> -n <app> -o table
az containerapp logs show -g <rg> -n <app> --follow
```

Usual causes: image pull failure (identity lacks `AcrPull`), a crash on startup, or a failing
health probe.

---

## Router

### 503 `HOSTED_AGENT_ENDPOINT not configured`

The env var is unset or empty. It must be the full **responses** endpoint including
`?api-version=v1`.

### 502 from `/chat`

The router surfaces the upstream body (truncated). Common causes:

| Upstream | Meaning |
|---|---|
| 424 `session_not_ready` | the hosted agent is crashing — see the hosted section |
| 401 / 403 | the router's managed identity lacks invoke rights on the project |
| 404 | wrong endpoint, or the agent version was deleted |

### 403 `not entitled to <agent>`

`ENTITLEMENTS` rejected the caller's groups. In the lab, `groups` defaults to `{"*"}` when empty.
In production it must come from a validated token.

### `/prewarm` returns 502

Check the URL derivation. Sessions hang off the agent endpoint root:

```
.../endpoint/sessions?api-version=v1                    ✓
.../endpoint/protocols/openai/sessions?api-version=v1   ✗ 404
```

---

## Environment quirks (Windows / PowerShell)

| Symptom | Cause | Fix |
|---|---|---|
| Garbled or crashing CLI output | cp1252 console | `$env:PYTHONUTF8="1"`, `$env:PYTHONIOENCODING="utf-8"` |
| Files with a BOM break parsers | `Set-Content -Encoding utf8` adds a BOM | `[System.IO.File]::WriteAllText($p, $s, (New-Object System.Text.UTF8Encoding($false)))` |
| Here-strings mangle quotes | PowerShell quoting | write a script file instead of inlining |
| `$_.Exception.Response.GetResponseStream()` missing | PowerShell 7 changed the model | use `$_.ErrorDetails.Message` |
| `az` extension install fails with `WinError 5` | permissions on the extensions directory | use `az rest` against the REST API instead |

---

## General diagnostic order

When an agent misbehaves, check in this order. It is roughly cheapest-first and matches how
often each was the actual cause in this lab.

1. **Is it a state bug or a logic bug?** Ask the same question twice in one session. Consistent
   wrong answers are logic; drifting answers are state.
2. **Which replica served it?** Different replicas plus local disk is the bug.
3. **Is the session attached?** Compare `agent_session_id` across turns. A changing value means
   a new sandbox each turn.
4. **Is it in the body or the header?** The header alone does nothing on turn one.
5. **Did the agent even start?** 424 means it crashed at import. Read the console logs.
6. **Is a policy reverting your config?** `az policy state list` before debugging any
   networking or auth issue in a governed tenant.
