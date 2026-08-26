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
opening a new sandbox every turn: check that the client stores `session_handle` and
`previous_response_id` from each response and sends both back on the next request.

> **Do not try to detect this from a field in the response.** We tried, and it does not work:
> the runtime **echoes back whatever `agent_session_id` you sent**, including ids it has never
> seen (64 characters of `a` come back verbatim). So a response that names your session proves
> nothing about whether your session was actually reattached. Latency is the only honest signal
> — which is why the table above is the diagnostic.

Measure it directly:

```powershell
$h  = @{ "x-user-id" = "probe-user" }
$pw = Invoke-RestMethod -Method Post "$env:HYBRID_URL/prewarm" -Headers $h

$t1 = Measure-Command {
  $script:r1 = Invoke-RestMethod -Method Post "$env:HYBRID_URL/chat" -Headers $h `
    -ContentType "application/json" `
    -Body (@{ session_id="probe"; message="Add Paris to my trip"
              session_handle=$pw.session_handle } | ConvertTo-Json)
}
$t2 = Measure-Command {
  Invoke-RestMethod -Method Post "$env:HYBRID_URL/chat" -Headers $h `
    -ContentType "application/json" `
    -Body (@{ session_id="probe"; message="What is in my trip?"
              session_handle=$r1.session_handle
              previous_response_id=$r1.previous_response_id } | ConvertTo-Json) | Out-Null
}
"turn1={0:n0}ms  turn2={1:n0}ms" -f $t1.TotalMilliseconds, $t2.TotalMilliseconds
# healthy:  turn1=10004ms  turn2=2413ms
# dropped:  turn1=9115ms   turn2=9634ms
```

If both turns are slow *and* the second answer has forgotten Paris, the session is being
dropped. If both are slow but the answer is correct, see "everything is slow" below — the model
deployment may simply be under contention.

Over a twenty-turn conversation this is the difference between 56 s and 193 s of cumulative
latency. See `time_router()` in `experiments/02_cold_start.py` for a correct client loop.

### `/chat` returns 503 "session signing is not configured"

The router refuses to run without `SESSION_SECRET`, deliberately: falling back to raw session
ids would silently remove the only thing separating one user's session from another's.

```powershell
Invoke-RestMethod "$env:HYBRID_URL/health" | Select-Object session_signing_configured
```

`False` almost always means the image was updated in place:

```powershell
az containerapp update -n aghl-hybrid-router --image ...:v8    # <- sets the image only
```

That creates neither the secret nor the environment variable. Deploy `infra/apps.bicep` with
`sessionSecret=`, or add them explicitly — see runbook 03, step 4.

### `/chat` returns 403 "session handle is not valid for this user"

Working as intended if the handle really does belong to somebody else. Otherwise, in order of
likelihood:

1. **The signing key differs between replicas.** If it is generated per process rather than
   supplied as a shared secret, the same handle succeeds or fails depending on which replica
   answers — roughly 50% of requests with two replicas. Confirm the value comes from
   `secretRef: 'session-secret'`.
2. **The key rotated under a live session.** Redeploying with a fresh `sessionSecret`
   invalidates every handle already in the wild. `scripts/deploy.ps1` reuses the router's
   existing secret for this reason.
3. **The `x-user-id` header changed mid-conversation** — or is missing, in which case the caller
   is `anonymous` and only matches other anonymous callers. In production this identity comes
   from a validated token, so the equivalent symptom is a token whose subject claim is not
   stable.

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

The env var is unset or empty. Either form of the endpoint works — the router normalises both:

```
https://<foundry>/api/projects/<p>/agents/<name>/endpoint/protocols/openai
https://<foundry>/api/projects/<p>/agents/<name>/endpoint/protocols/openai/responses?api-version=v1
```

If you see `API version not supported` instead, you concatenated the second form with a path and
produced `.../responses?api-version=v1/responses?api-version=v1`.

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

## Everything is slow, including the self-hosted controls

If *all four* targets slow down together — self-hosted included, and those never touch a
sandbox — the problem is not sessions. It is the shared model deployment.

We hit this after running eight rounds of the pre-warm experiment back to back: the next router
call took 10.8 s on turn 1 *and* 10.1 s on turn 2, which looks exactly like a dropped session.
Two repeats a minute later were normal (3.8 s / 2.6 s). Nothing had changed but the queue.

| Observation | Interpretation |
|---|---|
| Only hosted/hybrid slow, self-hosted fine | sandbox cold start — a real finding |
| All four slow together | model deployment contention — wait, or raise the TPM quota |
| Slow and it does not recover | check the deployment's rate limits before blaming the code |

Always re-measure before drawing a conclusion. A single timing on a shared model deployment is
an anecdote, which is why every experiment here takes a median over several rounds.

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
3. **Is the session attached?** Do *not* try to answer this from a response field — the runtime
   echoes back whatever session id you sent, so it always looks attached. Compare turn 1 and
   turn 2 latency instead: a warm turn 2 means attached.
4. **Is it in the body or the header?** The header alone does nothing on turn one.
5. **Did the agent even start?** 424 means it crashed at import. Read the console logs.
6. **Is everything slow, or just the hosted paths?** Everything means model contention, not
   sessions.
6. **Is a policy reverting your config?** `az policy state list` before debugging any
   networking or auth issue in a governed tenant.
