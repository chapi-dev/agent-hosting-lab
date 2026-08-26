# Agent Hosting Lab

**Self-hosted agents vs. Foundry Hosted Agents — settled with measurements, not opinions.**

This repository deploys *the same agent* four different ways on real Azure infrastructure,
runs experiments against all four, and reports what actually happened. Every number in this
README came out of a live deployment in Sweden Central. Nothing here is estimated.

The conclusion, stated up front so you can decide whether to keep reading:

> **Own the routing. Rent the runtime.**
>
> Run a thin, stateless, self-hosted layer for intent routing, authorization and private
> network access. Delegate the agent runtime — session state, sandboxing, scaling, identity,
> telemetry — to Foundry Hosted Agents. Pre-warm sessions to hide the one real cost of doing so.

---

## Why this lab exists

The question "should we self-host our agents or use hosted agents?" is usually answered with
a feature matrix. Feature matrices are easy to write and hard to trust, because they compare
documentation rather than behaviour.

So this lab does something different. It takes one agent — a trivial trip planner with two
tools — and deploys it four ways **without changing the agent code**. All four variants import
the same `agent_core` package. If the agent logic differed between variants, the comparison
would be measuring the code, not the hosting.

What differs is only what surrounds the agent. That turns out to be the entire story.

## The four variants

| Variant | What it is | Where it runs |
|---|---|---|
| `selfhosted-naive` | FastAPI + the agent, 2 replicas, state on local disk | Azure Container Apps |
| `selfhosted-hardened` | Same, but state in Cosmos DB behind a private endpoint | Azure Container Apps + VNet |
| `hosted-agent` | The agent, deployed to the Foundry managed runtime | Foundry Hosted Agents |
| `hybrid-router` | Stateless self-hosted router in front of the hosted agent | Container Apps → Foundry |

`selfhosted-naive` is not a straw man. Nothing in its configuration is obviously wrong: two
replicas is prudent, and writing session state to disk is the most natural thing a developer
does when the agent needs to remember something. The bug lives in the *combination*, and the
combination is what a team ships when nobody has been bitten by it yet.

## What we measured

### 1. Session state across replicas

Three turns: *"Add Paris to my trip"*, *"Add Rome"*, *"What is in my trip?"*

| Variant | Result | Distinct replicas hit | Answer to turn 3 |
|---|---|---|---|
| `selfhosted-naive` | ❌ **FAIL** | 2 | *"Your trip currently includes: Paris."* |
| `selfhosted-hardened` | ✅ PASS | 2 | *"Your trip is: Paris -> Rome."* |
| `hosted-agent` | ✅ PASS | n/a (platform-managed) | *"Your trip is: Paris -> Rome."* |
| `hybrid-router` | ✅ PASS | 2 | *"Your trip is: Paris -> Rome"* |

Read the naive failure carefully, because it is the most important result in this repository.
The agent did not crash. It did not time out. It did not return a 500. It answered the user's
question **fluently, immediately, and incorrectly**, and no health probe, no alert and no
integration test that checks for HTTP 200 would have caught it.

That is the shape of the risk self-hosting introduces: not outages, but confident wrong answers.

The failure and the fix use *the same class*. `agent_core.state.DiskStore` is a bug in
`selfhosted-naive` and is correct in `hosted-agent`. Identical code, opposite guarantees,
because the platform underneath changed. See [docs/02-state-and-sessions.md](docs/02-state-and-sessions.md).

### 2. Cold start (4 rounds, median)

| Variant | First turn | Warm turn | Penalty |
|---|---|---|---|
| `selfhosted-naive` | 1884 ms | 2048 ms | −164 ms |
| `selfhosted-hardened` | 2164 ms | 1939 ms | +225 ms |
| `hosted-agent` | **10218 ms** | 2400 ms | **+7818 ms** |
| `hybrid-router` | **10004 ms** | 2413 ms | **+7592 ms** |

This is the honest cost of hosted agents and the lab does not hide it. Starting a new session
means the platform provisions a sandbox, and that takes roughly eight seconds. Self-hosted
containers kept warm by minimum replicas do not pay it.

But note the second column. Once the session exists, the two models are within ~400 ms of each
other. The penalty is **per session, not per turn** — and it is paid when the session is
*created*, which need not be when the user sends their first message.

### 3. Pre-creating the session removes two thirds of that penalty

Provision the sandbox when the chat window opens, then send the user's first real message with
`agent_session_id` **in the request body**:

| Attachment mechanism | Session create | First real turn | Session reused |
|---|---|---|---|
| **body field `agent_session_id`** | 6558 ms | **3174 ms** | **100%** (4/4) |
| header `x-agent-session-id` | 6174 ms | 9458 ms | **0%** (0/4) |

**9.5 s becomes 3.2 s.** The 6.5 s of provisioning happens while the user reads the greeting.

The second row is the trap, and it is why this is a finding rather than a tip. Passing the
pre-created id in the `x-agent-session-id` **header** reattached zero times out of four: the
header is only honoured alongside `previous_response_id`, which by definition does not exist on
turn one — the exact turn pre-warming targets. The request succeeds, the answer is correct, and
the session you provisioned is silently discarded.

Verified end to end through the deployed router: `POST /prewarm` took 9728 ms (hidden), the
user's first real message returned in **3968 ms** with the session correctly reattached.

Priming with a throwaway `"hello"` message works too (~70% saved) but costs a model call.
Self-hosted variants were run as a control and correctly did not move.
See [docs/04-cold-start-and-scaling.md](docs/04-cold-start-and-scaling.md).

### 4. Deployment surface (lines of code you own and maintain)

| | Lines | Ratio to agent logic |
|---|---|---|
| Shared agent logic | 122 | 1.0x |
| **Self-hosted only** | **509** | 4.17x |
| **Hosted only** | **53** | 0.43x |

**Self-hosting costs 9.6x the supporting code** to run the same agent.

The 509 lines are not padding: a web server (84), a Dockerfile (25), pinned requirements (9),
Container Apps Bicep (255), a virtual network (96) and a VNet-injected environment (40). Every
line is something a person wrote, reviews, patches and gets paged about.

The hosted variant's 53 lines are a 20-line handler, four requirements and a 29-line
`azure.yaml`. See [docs/03-the-cost-of-self-hosting.md](docs/03-the-cost-of-self-hosting.md).

## The finding nobody predicted

Halfway through building this lab, `selfhosted-hardened` started returning HTTP 500:

```
CosmosHttpResponseError (Forbidden) Request originated from IP ... blocked by
your Cosmos DB account firewall settings
```

The account had `publicNetworkAccess: Disabled`. Re-enabling it via Bicep, then via
`az cosmosdb update`, then via `az rest PATCH`, all appeared to succeed — and all three were
silently reverted within seconds. The cause was an Azure Policy assignment
(`CosmosDB_PublicNetwork_Modify`, effect `modify`) rewriting the property on every write. A
second policy in the same assignment, `CosmosDB_LocalAuth_Modify`, had already disabled
key-based auth — so the code also needed `DefaultAzureCredential` plus a Cosmos data-plane role
assignment rather than a connection string.

IP allow-listing was not an option either: the Container Apps environment has **160+ outbound
IPs**, none of them stable.

So the self-hosted variant required a virtual network, a subnet, a private endpoint, a private
DNS zone, a VNet link, and a workload-profiles environment. And because a Container Apps
environment's VNet is immutable, both apps had to be **deleted and recreated** to get there.

**The Foundry hosted agent needed none of it.** It reached the platform's own data plane and
was never subject to the policy at all.

This is the part a feature matrix cannot tell you: in a governed enterprise tenant, the cost
of self-hosting is not the code you write. It is the org-wide controls that you become
responsible for satisfying.

## Recommendation

**Hybrid.** Not as a compromise, but because the two models fail in different places and the
hybrid is the only shape where neither failure lands on you.

```
    client
      │
      ▼
  ┌─────────────────────────────────────┐
  │  Router  (self-hosted, stateless)   │   ← you own this. 141 lines.
  │  • intent classification            │      Scales freely: no session state.
  │  • authorization / entitlements     │
  │  • private network egress           │
  └──────────────┬──────────────────────┘
                 │  x-agent-session-id + previous_response_id
                 ▼
  ┌─────────────────────────────────────┐
  │  Foundry Hosted Agent runtime       │   ← the platform owns this.
  │  • per-session sandbox + state      │      No Dockerfile, no VNet,
  │  • scaling, identity, telemetry     │      no state backend, no policy fights.
  └─────────────────────────────────────┘
```

The router is self-hosted because it does things the platform does not model: deciding which
agent handles a request, and applying authorization *before* routing. Those are real reasons to
run your own compute, and none of them are about running a model.

The runtime is hosted because session state, sandboxing, scaling, identity and telemetry are
solved problems, and solving them again costs 9.6x the code and a network topology.

The router holds no session state at all. It returns both session handles to the caller and
takes them back on the next turn. That is why it survives being spread across two replicas in
the experiment above while still passing the state test.

| Requirement | Self-hosted | Hosted | Hybrid |
|---|---|---|---|
| Session state across replicas | you build it | ✅ structural | ✅ structural |
| Authorization-aware routing | ✅ | ❌ | ✅ |
| Private network / on-prem backends | ✅ | ❌ | ✅ |
| Arbitrary Python business logic | ✅ | ✅ | ✅ |
| Custom guardrails before dispatch | ✅ | partial | ✅ |
| Scales without a state backend | ❌ | ✅ | ✅ |
| First-turn latency | ✅ ~1.9 s | ❌ ~10.2 s | ❌ ~10.0 s |
| First-turn latency, session pre-created | ~1.9 s | ✅ ~3.2 s | ✅ ~4.0 s |
| Lines of code you maintain | 509 | 53 | 194 (53 + 141) |

## Repository map

```
docs/           the reasoning, one argument per file
runbooks/       step-by-step production accelerators
src/agent_core/ the agent, shared verbatim by all four variants
src/selfhosted/ FastAPI server + hybrid router
src/hosted/     the Foundry hosted handler
infra/          Bicep: platform, apps, network, VNet environment
experiments/    the measurements, and their raw JSON results
scripts/        deploy and prepare helpers
```

### Documents

1. [The four variants](docs/01-the-four-variants.md) — what each one is, and why the comparison is fair
2. [State and sessions](docs/02-state-and-sessions.md) — the same class as bug and as feature; the two session identifiers
3. [The cost of self-hosting](docs/03-the-cost-of-self-hosting.md) — 9.6x, and the governance policy that caused most of it
4. [Cold start and scaling](docs/04-cold-start-and-scaling.md) — the 7.8 s penalty and how to make it disappear
5. [Choosing a hosting model](docs/05-choosing-a-hosting-model.md) — a decision procedure, not a feature table

### Runbooks

1. [Deploy a hosted agent](runbooks/01-deploy-hosted-agent.md) — including the five traps that cost this lab an evening
2. [Deploy a self-hosted agent](runbooks/02-deploy-self-hosted-agent.md)
3. [Deploy the hybrid router](runbooks/03-deploy-hybrid-router.md)
4. [Troubleshooting](runbooks/04-troubleshooting.md) — every error this lab hit, with its cause
5. [Teardown](runbooks/05-teardown.md)

## Reproducing this

Prerequisites: Azure CLI (logged in), `azd` ≥ 1.20, Python 3.11+, an Azure subscription.

```powershell
git clone <this-repo>
cd agent-hosting-lab
./scripts/deploy.ps1            # provisions everything, writes .env.lab
./scripts/run-experiments.ps1   # runs all three experiments
```

`deploy.ps1` is idempotent and takes roughly 25 minutes end to end, most of it waiting for
Foundry and the Container Apps environment. Results land in `experiments/results/` as
timestamped JSON.

## A note on scope

This lab measures **hosting**, deliberately. The agent is trivial on purpose: two tools, one
model call per turn, a deterministic classifier in the router. That keeps the experiments
repeatable and stops agent quality from contaminating hosting measurements.

What this lab does **not** measure: multi-agent orchestration at depth, evaluation pipelines,
prompt quality, or cost at scale. Those matter, and they are mostly orthogonal to the hosting
choice.

## Environment used

Sweden Central. Foundry project with `gpt-5.4-mini`, Log Analytics + Application Insights, a
user-assigned managed identity, ACR, serverless Cosmos DB, and a VNet-injected Container Apps
environment on workload profiles. All Bicep is in `infra/`.
