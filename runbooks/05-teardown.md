# Runbook 5 — Teardown

The lab deploys real, billable resources. Delete them when you are done.

---

## Everything at once

```powershell
az group delete -n rg-agenthost-lab --yes --no-wait
```

This removes the Foundry account and project, the model deployment, both self-hosted container
apps, the router, the Container Apps environment, the VNet and private endpoint, the private DNS
zone, ACR, Cosmos DB, Log Analytics and Application Insights.

Deletion takes 10–20 minutes. Foundry accounts are the slowest.

Then clean up the local `azd` environment:

```powershell
azd env list
azd env delete agent-hosting-lab-dev
```

> **Resource group names are not immediately reusable.** A deleting Foundry account holds its
> name until deletion completes, and recreating a group with the same name while the old one is
> still going fails in confusing ways. Use a new name, or wait.

---

## Selective teardown

### Just the hosted agent

```powershell
azd ai agent delete trip-planner
```

Versions are immutable and accumulate. List and clean them:

```powershell
azd ai agent show trip-planner
azd ai agent sessions list trip-planner
```

Sessions expire after 30 days on their own, but you can delete them explicitly:

```powershell
azd ai agent sessions delete trip-planner <session-id>
```

### Just the container apps

```powershell
az containerapp delete -g rg-agenthost-lab -n aghl-selfhosted-naive --yes
az containerapp delete -g rg-agenthost-lab -n aghl-selfhosted-hardened --yes
az containerapp delete -g rg-agenthost-lab -n aghl-hybrid-router --yes
```

Keeping the environment and deleting only the apps is the cheapest way to pause the self-hosted
side while keeping the hosted agent alive.

### Just the model deployment

The largest running cost if the lab sits idle:

```powershell
az cognitiveservices account deployment delete `
  -g rg-agenthost-lab -n <foundry-account> --deployment-name gpt-5.4-mini
```

---

## What costs money while idle

| Resource | Idle cost |
|---|---|
| Model deployment | none if unused (pay per token) |
| Container Apps, `minReplicas: 2` | **continuous** — the main idle cost |
| Container Apps environment (Consumption profile) | none |
| Container Apps environment (dedicated profile) | **per provisioned instance, always** |
| Cosmos DB serverless | per request + storage — negligible when idle |
| ACR (Basic) | small monthly fixed |
| Log Analytics | ingestion + retention |
| VNet, private endpoint | small hourly for the private endpoint |
| Foundry account / project | none |

**`minReplicas: 2` on three container apps is the bill.** To pause cheaply without deleting
anything:

```powershell
az containerapp update -g rg-agenthost-lab -n aghl-selfhosted-naive --min-replicas 0
az containerapp update -g rg-agenthost-lab -n aghl-selfhosted-hardened --min-replicas 0
az containerapp update -g rg-agenthost-lab -n aghl-hybrid-router --min-replicas 0
```

Note that this invalidates the cold-start experiment — scale-to-zero adds a container cold start
the measurements deliberately exclude. Set them back to 2 before re-running experiments.

---

## Verify it is gone

```powershell
az resource list -g rg-agenthost-lab -o table          # should error: group not found
az group list --query "[?starts_with(name,'rg-agenthost')].name" -o tsv
```

Check for orphans outside the group — private DNS zone links and role assignments occasionally
survive:

```powershell
az network private-dns zone list --query "[?contains(name,'documents')].{name:name, rg:resourceGroup}" -o table
az role assignment list --assignee <uami-principal-id> -o table
```

---

## Local cleanup

```powershell
Remove-Item -Recurse -Force .venv, .azure, src\hosted\agent_core
Remove-Item .env.lab
```

`src/hosted/agent_core/` is a generated copy of the shared agent, recreated by
`scripts/prepare-hosted.ps1`. It is gitignored.

Keep `experiments/results/` — those JSON files are the evidence behind every number in the docs,
and they are small.
