# Runbook 2 — Deploy a self-hosted agent

Running the same agent on Azure Container Apps. This is the longer path, and the runbook is
longer for exactly the reason [docs/03](../docs/03-the-cost-of-self-hosting.md) measures: there
is more to own.

Do this when you have a concrete reason — private network egress, a runtime the hosted platform
does not offer, or a hard sub-2-second first-turn requirement. If you do not have one, read
[runbook 3](03-deploy-hybrid-router.md) instead.

**Time:** ~45 minutes for the happy path. Longer if your tenant has governance policies, and it
probably does.

---

## Decide first: where does session state live?

Answer this before writing any code, because retrofitting it is how the failure in
[docs/02](../docs/02-state-and-sessions.md) happens.

| Replicas | State on local disk | Verdict |
|---|---|---|
| 1 | works | until you scale, or the pod restarts |
| ≥2 | **silently loses data** | never do this |

There is no configuration that makes local disk safe with more than one replica. If your agent
remembers anything across turns, you need a shared store.

This lab uses Cosmos DB serverless. Redis, PostgreSQL and Azure Table Storage are all fine. What
matters is that it is shared, durable, and has a TTL.

---

## Step 1 — Platform

```powershell
az group create -n rg-agenthost-lab -l swedencentral
az deployment group create -g rg-agenthost-lab -f infra/main.bicep -p prefix=aghl
```

Creates: Foundry account + project + model deployment, Log Analytics, Application Insights, a
user-assigned managed identity, ACR, and Cosmos DB.

## Step 2 — The server

`src/selfhosted/server.py` is 84 effective lines. All of it is work the hosted runtime does for
free:

```python
app = FastAPI()

def _store():
    if os.environ.get("STATE_BACKEND") == "cosmos":
        return CosmosStore(...)          # shared, correct across replicas
    return DiskStore(...)                # per-replica. Single replica only.

@app.post("/chat")
async def chat(req: ChatRequest):
    agent = build_agent(_store(), req.session_id)   # session id is load-bearing
    result = await agent.run(req.message)
    return {"reply": result.text, "replica": REPLICA, ...}

@app.get("/health")
async def health():
    return {"status": "ok", "replica": REPLICA}
```

Two things to notice:

- **`req.session_id` is a security boundary.** A caller who can send an arbitrary session id can
  read another user's conversation. Derive it from a validated token, never trust it from the
  request body. This lab trusts it because it is a lab.
- **`replica` in the response.** Not decoration — it is how you see cross-replica bugs instead
  of guessing at them.

## Step 3 — Telemetry

The hosted runtime does this for you. Here it is yours:

```python
from azure.monitor.opentelemetry import configure_azure_monitor

def _configure_telemetry():
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn:
        configure_azure_monitor(connection_string=conn)
```

> `azure-monitor-opentelemetry` is available up to **1.8.9**. Pinning 1.9.0 fails the build with
> a resolution error that does not name the package clearly.

## Step 4 — Dockerfile

```dockerfile
FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PORT=8000
WORKDIR /app
COPY selfhosted/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
EXPOSE 8000
CMD ["uvicorn", "selfhosted.server:app", "--host", "0.0.0.0", "--port", "8000"]
```

This is now yours to patch forever. Every base-image CVE is your ticket.

## Step 5 — Build

```powershell
az acr build --registry <acr-name> `
  --image agent-hosting-lab:v1 `
  --file src/Dockerfile ./src `
  --no-logs
```

Two things that will bite you:

- **The build context is `./src`, not `.`** — the `COPY selfhosted/requirements.txt` line is
  relative to the context. Getting this wrong fails with
  `COPY failed: file not found in build context`.
- **`--no-logs` is not optional on Windows.** Without it the Azure CLI crashes with
  `UnicodeEncodeError` (cp1252) while streaming build output. The build itself still succeeds,
  which makes it look like a build failure when it is a console encoding failure. Check with
  `az acr task logs -r <acr> --run-id <id>`.

## Step 6 — Deploy

```powershell
az deployment group create -g rg-agenthost-lab -f infra/apps.bicep `
  -p prefix=aghl imageTag=v1
```

`infra/apps.bicep` is 255 lines. The parts that matter:

```bicep
// The naive variant. Nothing here is individually wrong.
minReplicas: 2                  // prudent
env: [ { name: 'STATE_BACKEND', value: 'disk' } ]   // natural
// The bug is the combination.
```

```bicep
// The hardened variant.
minReplicas: 2
env: [ { name: 'STATE_BACKEND', value: 'cosmos' } ]
```

Identity and registry access:

```bicep
identity: { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }
registries: [ { server: acr.properties.loginServer, identity: uami.id } ]
```

## Step 7 — Verify, properly

Health checks are not enough. Health was green throughout the failure this lab reproduced.

```powershell
$u = "https://<app>.<region>.azurecontainerapps.io"
Invoke-RestMethod "$u/health"

$s = "verify-$(Get-Random)"
1..3 | ForEach-Object {
  $msgs = @("Add Paris to my trip", "Add Rome", "What is in my trip?")
  $r = Invoke-RestMethod -Method Post "$u/chat" -ContentType application/json `
        -Body (@{session_id=$s; message=$msgs[$_-1]} | ConvertTo-Json)
  "{0}  [{1}]" -f $r.reply, $r.replica
}
```

**Turn 3 must mention both cities, and you must see more than one distinct replica.** If every
turn hits the same replica, the test proved nothing — run it again until it spreads.

`experiments/01_session_state.py` automates this and reports the replica spread.

---

## The governance detour

This is where a self-hosted deployment stops being about code. Read
[docs/03](../docs/03-the-cost-of-self-hosting.md) for the full account; this is the operational
version.

### Symptom

```
CosmosHttpResponseError (Forbidden) Request originated from IP <ip>,
blocked by your Cosmos DB account firewall settings.
```

### Do not start by "fixing" the firewall

You will appear to succeed and change nothing. Check for a `modify` policy first:

```powershell
$cosmos = az cosmosdb list -g rg-agenthost-lab --query "[0].id" -o tsv
az policy state list --resource $cosmos `
  --query "[?policyDefinitionAction=='modify'].{policy:policyDefinitionName, assign:policyAssignmentName}" -o table
```

```
Policy                         Assign
-----------------------------  ----------------------
CosmosDB_PublicNetwork_Modify  MCAPSGovDeployPolicies
CosmosDB_LocalAuth_Modify      MCAPSGovDeployPolicies
```

A `modify` policy rewrites the property on **every** write. Bicep, `az cosmosdb update` and
`az rest PATCH` all report success and are all reverted within seconds. Nothing fails, so
nothing tells you why.

The second policy disables key-based auth, so you also cannot use a connection string:

```powershell
az cosmosdb show --ids $cosmos --query "{localAuth:disableLocalAuth, pna:publicNetworkAccess}"
# { "localAuth": true, "pna": "Disabled" }
```

### IP allow-listing is not the answer

A Container Apps environment does not have one outbound IP. This one reported **160+**, and they
are not stable. Allow-listing them is a future incident, not a control.

### The actual answer: VNet + private endpoint

```powershell
az deployment group create -g rg-agenthost-lab -f infra/network.bicep -p prefix=aghl
az deployment group create -g rg-agenthost-lab -f infra/environment-vnet.bicep -p prefix=aghl
```

Creates a VNet, an infrastructure subnet (**/23 minimum** — smaller is rejected), a
private-endpoint subnet, a private endpoint to Cosmos, the `privatelink.documents.azure.com`
private DNS zone, and a VNet link.

### The immutability tax

**A Container Apps environment's VNet cannot be changed after creation.** Moving existing apps
into the new environment fails:

```
ContainerAppEnvironmentMismatch
```

You must delete and recreate the apps:

```powershell
az containerapp delete -g rg-agenthost-lab -n aghl-selfhosted-naive --yes
az containerapp delete -g rg-agenthost-lab -n aghl-selfhosted-hardened --yes
az deployment group create -g rg-agenthost-lab -f infra/apps.bicep `
  -p prefix=aghl imageTag=v1 environmentName=aghl-cae-vnet
```

In a lab this is an annoyance. In production with live traffic it is a migration project with a
maintenance window. **Plan VNet integration before the first deploy, not after.**

### Managed identity for Cosmos data plane

With local auth disabled you need an explicit data-plane role assignment. This is separate from
ARM RBAC and is a common source of confusion:

```powershell
az cosmosdb sql role assignment create `
  -g rg-agenthost-lab -a <cosmos-account> `
  --role-definition-id "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.DocumentDB/databaseAccounts/<acct>/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002" `
  --principal-id <uami-principal-id> `
  --scope "/"
```

`...0002` is the built-in **Cosmos DB Built-in Data Contributor**.

---

## Operating it

### Scaling

```bicep
scale: {
  minReplicas: 2          // latency vs cost
  maxReplicas: 10         // throughput vs blast radius
  rules: [ { name: 'http', http: { metadata: { concurrentRequests: '20' } } } ]
}
```

`minReplicas: 0` scales to zero and costs you a container cold start on the first request. It
also does not remove the state problem — it makes it worse, because a fresh replica has no disk
state at all.

### Logs

```powershell
az containerapp logs show -g rg-agenthost-lab -n aghl-selfhosted-hardened --follow
```

### Rollout

```powershell
az containerapp update -g rg-agenthost-lab -n aghl-selfhosted-hardened `
  --image <acr>.azurecr.io/agent-hosting-lab:v2
```

Unlike hosted agents, you *do* get revision traffic splitting here. It is one of the genuine
advantages of self-hosting.

---

## Checklist

- [ ] State backend chosen before writing code, and it is shared
- [ ] Never local disk with `minReplicas` > 1
- [ ] Session id derived from a validated token, never trusted from the body
- [ ] Replica id in every response, so cross-replica bugs are visible
- [ ] `azure-monitor-opentelemetry` ≤ 1.8.9
- [ ] Build context is `./src`; `--no-logs` on Windows
- [ ] `az policy state list` checked **before** debugging any firewall issue
- [ ] VNet decided before the first deploy (it is immutable)
- [ ] Infrastructure subnet /23 or larger
- [ ] Cosmos data-plane role assigned to the managed identity
- [ ] Multi-replica continuity test in CI, not just a health check
- [ ] TTL configured on the state store
