# Runbook 3 — Deploy the hybrid router

The recommended pattern: a thin, stateless, self-hosted router in front of a Foundry hosted
agent runtime.

**Own the routing. Rent the runtime.**

**Time:** ~15 minutes on top of [runbook 1](01-deploy-hosted-agent.md).

---

## What the router is for

The router exists for the things the hosted platform does not model. Not for running agents.

| The router does | Because |
|---|---|
| Intent classification | choosing between several agents is your product logic |
| Authorization / entitlements | who may reach which agent is your policy, and must be auditable |
| Guardrails before dispatch | some requests must never reach a model at all |
| Private network egress | tool calls into on-premises systems must leave from your network |
| Header injection for backends | per-session backend auth is yours to manage |

| The router does **not** do | Because |
|---|---|
| Hold session state | the hosted runtime does it better, and state would stop the router scaling |
| Run the agent | the hosted runtime does it |
| Manage identity or telemetry | the platform injects both |

**The single rule: the router must stay stateless.** The moment it remembers a session it needs
a database, a partition key and a TTL — and you have reintroduced self-hosting economics through
the side door.

---

## Step 1 — Deploy the hosted agent

Follow [runbook 1](01-deploy-hosted-agent.md). Keep the responses endpoint:

```powershell
azd ai agent show trip-planner --query "Endpoint (responses)"
```

## Step 2 — Write the router

`src/selfhosted/router.py`, 110 effective lines. The important parts:

```python
ROUTES = { "trip": "trip-planner" }

ENTITLEMENTS = { "trip-planner": {"*"} }     # replace with real groups

def classify(message: str) -> str:
    ...                                      # a model or a classifier service

def authorize(agent: str, groups: set[str]) -> bool:
    allowed = ENTITLEMENTS.get(agent, set())
    return "*" in allowed or bool(allowed & groups)
```

Adding the second agent is a dictionary entry and a deployment — not an architecture change.
That is the property that makes this shape future-proof.

### Forwarding a turn

```python
payload = {"input": req.message, "store": True}
if req.agent_session_id:
    payload["agent_session_id"] = req.agent_session_id        # body, not header
if req.previous_response_id:
    payload["previous_response_id"] = req.previous_response_id

resp = await http.post(f"{endpoint}", headers=auth, json=payload)
body = resp.json()

return {
    "reply": extract_text(body),
    "agent_session_id": resp.headers.get("x-agent-session-id")
                        or body.get("agent_session_id")
                        or req.agent_session_id,
    "previous_response_id": body.get("id"),
    "routed_to": agent,
    "replica": REPLICA,
}
```

Both handles go **out** to the caller and come **back** on the next request. The router keeps
nothing, so every replica is interchangeable.

> `agent_session_id` belongs in the **body**. The `x-agent-session-id` header is only honoured
> alongside `previous_response_id`, so on turn one it is ignored and the runtime allocates a
> fresh sandbox. Measured: body 4/4 reattached at 3174 ms, header 0/4 at 9458 ms.

### The pre-warm endpoint

```python
@app.post("/prewarm")
async def prewarm() -> dict:
    url = endpoint.split("/protocols/")[0] + "/sessions?api-version=v1"
    resp = await http.post(url, headers=auth, json={})
    return {"agent_session_id": resp.json()["agent_session_id"], ...}
```

Call it when the chat window opens. Measured on the deployed router: pre-warm 9728 ms (hidden
from the user), first real turn **3968 ms** with the session correctly reattached.

## Step 3 — Grant the router access to the agent

The router calls the runtime with its managed identity:

```bicep
env: [
  { name: 'HOSTED_AGENT_ENDPOINT', value: hostedAgentEndpoint }
  { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
]
```

The identity needs a role that permits invoking the Foundry project (`Azure AI User` or
equivalent). Token scope: `https://ai.azure.com/.default`.

## Step 4 — Build and deploy

```powershell
az acr build --registry <acr> --image agent-hosting-lab:v5 --file src/Dockerfile ./src --no-logs

az deployment group create -g rg-agenthost-lab -f infra/apps.bicep `
  -p prefix=aghl imageTag=v5 hostedAgentEndpoint=<responses-endpoint>
```

Or update in place:

```powershell
az containerapp update -g rg-agenthost-lab -n aghl-hybrid-router `
  --image <acr>.azurecr.io/agent-hosting-lab:v5
```

## Step 5 — Verify

```powershell
$u = "https://<router>.<region>.azurecontainerapps.io"

Invoke-RestMethod "$u/health"
# state_backend: "none (delegated to hosted agent)"   <- the point of the pattern

$pw = Invoke-RestMethod -Method Post "$u/prewarm"

$r1 = Invoke-RestMethod -Method Post "$u/chat" -ContentType application/json -Body (@{
  session_id="v1"; message="Add Paris to my trip"; agent_session_id=$pw.agent_session_id
} | ConvertTo-Json)

$r2 = Invoke-RestMethod -Method Post "$u/chat" -ContentType application/json -Body (@{
  session_id="v1"; message="What is in my trip?"
  agent_session_id=$r1.agent_session_id; previous_response_id=$r1.previous_response_id
} | ConvertTo-Json)

"$($r2.reply)  [replicas: $($r1.replica) / $($r2.replica)]"
# -> Your trip is: Paris -> Rome  [replicas: ...-kt79c / ...-vgc44]
```

**A conversation that spans two different replicas and still remembers is the whole result.**
`experiments/01_session_state.py` asserts it and reports the replica spread.

---

## The client contract

```
POST /prewarm                       -> { agent_session_id, expires_at }

POST /chat
{
  "session_id": "<your app's session>",
  "message": "...",
  "groups": ["hr-employees"],
  "agent_session_id": "<from /prewarm or previous reply>",
  "previous_response_id": "<from previous reply, omit on turn 1>"
}
                                    -> { reply, agent_session_id,
                                         previous_response_id, routed_to, replica }
```

Client responsibilities:

1. Call `/prewarm` when the UI opens; store `agent_session_id`.
2. Send it with the first message.
3. Store **both** handles from every reply and send both on the next turn.
4. On `/prewarm` failure, just omit `agent_session_id` — the conversation still works, only
   slower. Pre-warming is an optimisation, never a dependency.

---

## Adding a second agent

```python
ROUTES = {
    "trip": "trip-planner",
    "hr":   "hr-assistant",
}

ENTITLEMENTS = {
    "trip-planner": {"*"},
    "hr-assistant": {"hr-employees", "hr-admins"},
}
```

Then deploy the new hosted agent per [runbook 1](01-deploy-hosted-agent.md) and give the router
its endpoint. No infrastructure change.

To route to a **self-hosted** agent instead — because that one genuinely needs private egress —
add a branch that posts to a container URL rather than a Foundry endpoint. The routing layer
already exists, so mixed hosting is a code change, not a redesign. **This is what "future-proof"
means concretely.**

---

## Production hardening

The lab version is deliberately minimal. Before production:

| Area | Lab | Production |
|---|---|---|
| `groups` | trusted from the request body | derived from a validated JWT |
| `classify()` | keyword matching | a small model or classifier service |
| `ENTITLEMENTS` | in code | from Entra ID groups or a policy store |
| Ingress | public | behind APIM or App Gateway with WAF |
| Rate limiting | none | per user and per agent |
| Guardrails | none | content safety before dispatch |
| Token acquisition | per request | cached with refresh |
| Errors | pass through | sanitised; never leak backend detail |

**`groups` from the request body is the one that matters.** In the lab a caller can claim any
group. In production it must come from a validated token, or your authorization layer is
decorative.

---

## Why not put the router in APIM?

You can, and for rate limiting, WAF and token metrics you probably should — in front of the
router, not instead of it.

APIM is excellent at policy. It is awkward at intent classification, entitlement lookups against
a directory, and arbitrary Python business logic. Use both: APIM for the gateway concerns, the
router for the decisions that need code.

---

## Checklist

- [ ] Router is stateless — no session map, no database
- [ ] Both handles returned to the caller and required back
- [ ] `agent_session_id` sent in the **body**
- [ ] `/prewarm` exposed and called when the UI opens
- [ ] `groups` derived from a validated token in production
- [ ] Entitlements checked **before** dispatch
- [ ] Router identity has invoke rights on the Foundry project
- [ ] Verified across ≥2 replicas
- [ ] Backend errors sanitised before returning
