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
user = identify(x_user_id)                                    # see "Session handles" below

payload = {"input": req.message, "store": True}
if req.session_handle:
    payload["agent_session_id"] = open_handle(req.session_handle, user)   # body, not header
if req.previous_response_id:
    payload["previous_response_id"] = req.previous_response_id

resp = await http.post(f"{endpoint}", headers=auth, json=payload)
body = resp.json()

agent_session_id = (resp.headers.get("x-agent-session-id")
                    or body.get("agent_session_id"))

return {
    "reply": extract_text(body),
    "session_handle": issue_handle(agent_session_id, user) if agent_session_id
                      else req.session_handle,
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

### Why the router issues session handles

Notice that the client never sees `agent_session_id`; it sees a **handle**. That is not
decoration, and it is the one piece of security work the hybrid pattern adds.

We verified against the deployed runtime (`experiments/06_session_isolation.py`) that a session
id is *sufficient on its own* to read that session's state — no conversation history needed —
and that the runtime accepts any id a caller invents. That is fine when each end user calls with
their own credentials. It is not fine here: **the router calls the runtime with one managed
identity on behalf of every user**, so the runtime can no longer tell your users apart. Pass a
client-supplied session id straight through and any user who learns another's id reads their
session.

```python
def issue_handle(agent_session_id: str, user: str) -> str:
    signature = hmac.new(
        SESSION_SECRET, f"{agent_session_id}|{user}".encode(), hashlib.sha256
    ).digest()
    return f"{agent_session_id}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def open_handle(handle: str, user: str) -> str:
    agent_session_id, _, _ = handle.rpartition(".")
    # compare_digest, not ==: a signature check on attacker-supplied input.
    if not agent_session_id or not hmac.compare_digest(handle, issue_handle(agent_session_id, user)):
        raise HTTPException(status_code=403, detail="session handle is not valid for this user")
    return agent_session_id
```

Four things to get right:

1. **`SESSION_SECRET` must be shared by every replica.** Generate it per process and requests
   fail 403 at random, in proportion to your replica count. Deploy it as a Container Apps secret
   (`infra/apps.bicep` does this) and never as a plain env var.
2. **Derive `user` from a validated token in production.** The lab reads an `x-user-id` header
   because that is easy to exercise; a header is trivially forged. Use the object id from a JWT
   verified by Easy Auth, API Management, or the router itself. The same applies to `groups` —
   entitlements are only as trustworthy as the claim behind them.
3. **`/prewarm` must return the handle, never the raw id.** Leaking it once defeats the whole
   mechanism.
4. **Fail closed.** With no secret configured the router returns 503 rather than falling back to
   raw ids, and `/health` reports `session_signing_configured` so the cause is obvious.

Verification is a hash, so this costs no database and the router stays stateless.

### The pre-warm endpoint

```python
@app.post("/prewarm")
async def prewarm(x_user_id: str | None = Header(default=None)) -> dict:
    url = endpoint.split("/protocols/")[0] + "/sessions?api-version=v1"
    resp = await http.post(url, headers=auth, json={})
    return {"session_handle": issue_handle(resp.json()["agent_session_id"], identify(x_user_id)), ...}
```

Call it when the chat window opens. Measured on the deployed router: pre-warm 9728 ms (hidden
from the user), first real turn **3968 ms** with the session correctly reattached.

## Step 3 — Grant the router access to the agent

The router calls the runtime with its managed identity:

```bicep
secrets: [
  { name: 'session-secret', value: sessionSecret }
]
env: [
  { name: 'HOSTED_AGENT_ENDPOINT', value: hostedAgentEndpoint }
  { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
  { name: 'SESSION_SECRET', secretRef: 'session-secret' }
]
```

The identity needs a role that permits invoking the Foundry project (`Azure AI User` or
equivalent). Token scope: `https://ai.azure.com/.default`.

> `HOSTED_AGENT_ENDPOINT` exists in two forms and it is easy to be handed the wrong one:
> `.../protocols/openai` (what the router wants) and
> `.../protocols/openai/responses?api-version=v1` (what `azd env get-value
> AGENT_<NAME>_RESPONSES_ENDPOINT` returns). Concatenating the second yields
> `.../responses?api-version=v1/responses?api-version=v1` and a misleading *API version not
> supported*. The router normalises both, but know which one you are looking at.

## Step 4 — Build and deploy

```powershell
az acr build --registry <acr> --image agent-hosting-lab:v8 --file src/Dockerfile ./src --no-logs

az deployment group create -g rg-agenthost-lab -f infra/apps.bicep `
  -p prefix=aghl imageTag=v8 hostedAgentEndpoint=<protocol-base> sessionSecret=<shared-secret>
```

> Do **not** update in place with `az containerapp update --image` for this change.
> That updates the image but creates neither the `session-secret` secret nor the
> `SESSION_SECRET` variable, and the router will start and answer `/health` while failing every
> `/chat` with 503. Deploy the template, or add the secret explicitly:
>
> ```powershell
> az containerapp secret set -g rg-agenthost-lab -n aghl-hybrid-router `
>   --secrets session-secret=<shared-secret>
> az containerapp update -g rg-agenthost-lab -n aghl-hybrid-router `
>   --set-env-vars SESSION_SECRET=secretref:session-secret
> ```

`scripts/deploy.ps1` reuses the secret already on the router if there is one, so redeploying does
not invalidate handles issued minutes earlier.

## Step 5 — Verify

```powershell
$u = "https://<router>.<region>.azurecontainerapps.io"

Invoke-RestMethod "$u/health"
# state_backend: "none (delegated to hosted agent)"   <- the point of the pattern
# session_signing_configured: True                    <- else every /chat returns 503

$alice = @{ "x-user-id" = "alice" }

$pw = Invoke-RestMethod -Method Post "$u/prewarm" -Headers $alice

$r1 = Invoke-RestMethod -Method Post "$u/chat" -Headers $alice -ContentType application/json -Body (@{
  session_id="v1"; message="Add Paris to my trip"; session_handle=$pw.session_handle
} | ConvertTo-Json)

$r2 = Invoke-RestMethod -Method Post "$u/chat" -Headers $alice -ContentType application/json -Body (@{
  session_id="v1"; message="What is in my trip?"
  session_handle=$r1.session_handle; previous_response_id=$r1.previous_response_id
} | ConvertTo-Json)

"$($r2.reply)  [replicas: $($r1.replica) / $($r2.replica)]"
# -> Your trip is: Paris -> Rome  [replicas: ...-kt79c / ...-vgc44]
```

**A conversation that spans two different replicas and still remembers is the whole result.**
`experiments/01_session_state.py` asserts it and reports the replica spread.

Then confirm the handle is actually bound to Alice:

```powershell
Invoke-WebRequest -Method Post "$u/chat" -Headers @{ "x-user-id" = "mallory" } `
  -ContentType application/json -SkipHttpErrorCheck -Body (@{
    session_id="v1"; message="What is in my trip?"; session_handle=$r1.session_handle
  } | ConvertTo-Json) | Select-Object StatusCode
# -> 403
```

A 200 here means the router is handing every user the same door key.
`experiments/06_session_isolation.py` automates both halves.

---

## The client contract

```
POST /prewarm       (header: x-user-id)     -> { session_handle, expires_at }

POST /chat          (header: x-user-id)
{
  "session_id": "<your app's session>",
  "message": "...",
  "groups": ["hr-employees"],
  "session_handle": "<from /prewarm or previous reply>",
  "previous_response_id": "<from previous reply, omit on turn 1>"
}
                                    -> { reply, session_handle,
                                         previous_response_id, routed_to, replica }
```

Client responsibilities:

1. Call `/prewarm` when the UI opens; store `session_handle`.
2. Send it with the first message.
3. Store **both** handles from every reply and send both on the next turn.
4. On `/prewarm` failure, just omit `session_handle` — the conversation still works, only
   slower. Pre-warming is an optimisation, never a dependency.

The handle is opaque and bound to the calling user: it is safe to hold in the browser, and
useless to anyone else.

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
- [ ] `agent_session_id` sent to the runtime in the **body**
- [ ] Clients receive a **signed handle**, never a raw `agent_session_id`
- [ ] `SESSION_SECRET` deployed as a secret and **shared across replicas**
- [ ] Handles bound to a user identity taken from a **validated token**, not a header
- [ ] A handle issued to one user returns **403** for another (test it)
- [ ] `/prewarm` exposed and called when the UI opens
- [ ] `groups` derived from a validated token in production
- [ ] Entitlements checked **before** dispatch
- [ ] Router identity has invoke rights on the Foundry project
- [ ] Verified across ≥2 replicas
- [ ] Backend errors sanitised before returning
