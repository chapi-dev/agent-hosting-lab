# 3. The cost of self-hosting

The headline number is **9.6x**: self-hosting the same agent costs 9.6 times as much supporting
code as hosting it. That number is measured, not estimated — `experiments/03_deployment_surface.py`
counts effective lines (comments and blanks excluded) in the real files that were really deployed.

But the line count is the *smaller* half of the story. The larger half is what you become
responsible for, and this lab found that out by accident.

---

## The measured surface

| | Lines | Files |
|---|---|---|
| **Shared agent logic** | **122** | `agent_core/agent.py` (30), `state.py` (79), `__init__.py` (13) |
| **Self-hosted only** | **509** | `server.py` (84), `Dockerfile` (25), `requirements.txt` (9), `apps.bicep` (255), `network.bicep` (96), `environment-vnet.bicep` (40) |
| **Hosted only** | **53** | `main.py` (20), `requirements.txt` (4), `azure.yaml` (29) |
| Shared infrastructure | 222 | `main.bicep` — Foundry, model, observability, identity |
| Hybrid router | 141 | `router.py` |

`main.bicep` is attributed to *shared* deliberately. Both models need a Foundry project, a model
deployment, Application Insights and a managed identity. Counting it against self-hosting would
inflate the case, and the case does not need inflating.

Ratios: self-hosted support code is **4.17x** the agent itself. Hosted support code is **0.43x**.
The multiplier between the two is **9.6x**.

## What those 509 lines actually are

It is tempting to read "509 lines of YAML and Bicep" as trivial. It is not, because each block
encodes a decision that someone had to make correctly:

- **`server.py` (84)** — request model, session extraction, agent construction per request,
  error handling, health endpoint, replica reporting, OpenTelemetry wiring. The telemetry alone
  is a dependency, a connection-string lookup and an instrumentation call that the hosted runtime
  performs for you.
- **`Dockerfile` (25)** — base image choice, layer ordering, dependency install, non-root user,
  port, entrypoint. This is also a standing patch obligation: every base image CVE is now yours.
- **`requirements.txt` (9)** — pinned, and therefore something to keep pinned.
- **`apps.bicep` (255)** — two container apps, ingress, scale rules, identity assignment, ACR
  pull role, environment variables, secrets, probes, resource limits.
- **`network.bicep` (96)** — VNet, two subnets, private endpoint, private DNS zone, VNet link,
  DNS zone group.
- **`environment-vnet.bicep` (40)** — a second Container Apps environment, because the first
  one's VNet configuration could not be changed.

Every line is something a human wrote, another human reviewed, and somebody gets paged about.

## The part that is not code: governance

This lab was built in a normal enterprise-governed Azure subscription. Halfway through,
`selfhosted-hardened` began returning HTTP 500:

```
CosmosHttpResponseError (Forbidden) Request originated from IP <ip>, blocked by
your Cosmos DB account firewall settings.
```

The obvious fix is to enable public network access. It did not work — three times.

| Attempt | Result |
|---|---|
| Bicep `publicNetworkAccess: 'Enabled'` | deployment succeeded, property still `Disabled` |
| `az cosmosdb update --public-network-access ENABLED` | command succeeded, reverted within seconds |
| `az rest --method PATCH` on the ARM resource | 200 OK, reverted within seconds |

Three different tools, three apparent successes, zero effect. The `networkAclBypass` property
*did* change, which made it look like the deployment was working and the problem was elsewhere.

The cause was found here:

```powershell
az policy state list --resource $cosmosId --query "[?policyDefinitionAction=='modify'].{policy:policyDefinitionName, effect:policyDefinitionAction, assign:policyAssignmentName}" -o table
```

```
Policy                         Effect    Assign
-----------------------------  --------  ----------------------
CosmosDB_PublicNetwork_Modify  modify    MCAPSGovDeployPolicies
CosmosDB_LocalAuth_Modify      modify    MCAPSGovDeployPolicies
```

An Azure Policy assignment with effect `modify` rewrites the property on **every** write to the
resource. It is not a deny — the deployment succeeds, which is exactly why it is so confusing.
Nothing fails; the value simply is not what you set.

The second policy in the same assignment had already disabled key-based authentication. So the
code could not use a connection string either: it needed `DefaultAzureCredential` plus an
explicit Cosmos SQL data-plane role assignment for the managed identity.

```powershell
az cosmosdb show --ids $cosmosId --query "{localAuth:disableLocalAuth, pna:publicNetworkAccess}"
# { "localAuth": true, "pna": "Disabled" }
```

### Why IP allow-listing was not the escape hatch

The natural next thought is to add the container's outbound IP to the firewall instead. That
fails for a structural reason: an Azure Container Apps environment does not have *an* outbound
IP. This one reported **more than 160**, and they are not contractually stable.

Allow-listing 160 addresses that may change is not a security control. It is a future incident.

### So: a virtual network

The only remaining path was the correct one all along:

```
VNet
├── subnet: infrastructure (/23 minimum — smaller is rejected)
├── subnet: private-endpoints
├── private endpoint → Cosmos
├── private DNS zone: privatelink.documents.azure.com
└── VNet link
```

Plus a Container Apps environment injected into that VNet. Which brought its own surprise.

### The immutability tax

A Container Apps environment's VNet configuration **cannot be changed after creation**. Moving
the existing apps into the new environment failed with:

```
ContainerAppEnvironmentMismatch
```

The only path forward was to **delete both container apps** and recreate them in the new
environment. In this lab that was a minor annoyance. In production, with real traffic, that is a
migration project with a maintenance window.

Related constraints worth knowing before you commit:

- VNet injection requires a **workload profiles** environment. A Consumption-only profile still
  has no idle charge, but any dedicated profile is billed per provisioned instance regardless of
  load.
- The infrastructure subnet must be **/23 or larger**, and it is delegated — you cannot reuse it.

### What the hosted agent needed for the same problem

Nothing.

It reached the platform's own managed data plane. It was never subject to the Cosmos policy,
because it never needed a Cosmos account. There was no VNet to design, no subnet sizing to get
right, no private DNS zone to link, no environment to recreate.

**This is the finding a feature matrix cannot give you.** The cost of self-hosting in a governed
tenant is not primarily the code. It is that you inherit responsibility for satisfying every
org-wide control that applies to the resources you chose to own — including controls that are
invisible until they silently revert your deployment.

## Costs that never appear in a line count

Neither the 509 lines nor the policy story captures these, and they are permanent:

| Obligation | Self-hosted | Hosted |
|---|---|---|
| Base image CVE patching | yours, forever | platform |
| Runtime/SDK upgrades | yours | platform |
| Scaling rules and tuning | yours | platform |
| Health probes and liveness | yours | platform |
| Telemetry wiring | yours (a dependency + code) | injected |
| Session isolation correctness | yours (security boundary) | structural |
| Network topology and DNS | yours | none |
| Policy compliance for owned resources | yours | platform's |
| Onboarding a new developer | replica model, state model, network | write a handler |

The last row deserves emphasis. Every developer who touches a self-hosted agent must understand
that state is per-replica, that the session id is a security boundary, and that the network is
private. That is a permanent tax on every future hire and every future feature.

## Where the money actually goes

At small scale, self-hosting a Container App looks cheaper than per-token hosted pricing, and
teams frequently choose on that basis.

This lab suggests that comparison is measuring the wrong thing. The dominant costs are:

1. Engineering time to build the 509 lines (days).
2. Engineering time to satisfy governance (this lab: an evening, after three failed attempts, in
   a subscription with cooperative policies and no change-control board).
3. The permanent maintenance obligations in the table above.
4. The incident cost of getting session isolation wrong once.

Item 4 alone can exceed the compute savings for the lifetime of the project.

## When self-hosting is still correct

This document argues in one direction, so here is the honest other side. Self-host when:

- You need **private network egress** to systems that are not reachable from the platform's data
  plane — on-premises APIs, private endpoints, IP-restricted third parties.
- You need **latency below ~2 s on the first turn of a new session** and cannot pre-warm. See
  [04-cold-start-and-scaling.md](04-cold-start-and-scaling.md).
- You need a runtime, framework or library version the hosted runtime does not offer.
- You have **regulatory requirements** that mandate control over the execution environment.
- Your orchestration genuinely cannot be expressed as agents and tools.

Note that the first, third and fifth of those apply to the **router**, not to the agent runtime.
That is the argument for the hybrid: put the compute you genuinely need to own in a thin
stateless layer, and rent the rest.
