# Agent Hosting Lab

A controlled comparison of self-hosted and Foundry Hosted Agents on Azure, based on measurements
taken from live deployments rather than feature documentation.

The lab deploys *the same agent* four ways, runs seven experiments against all four variants, and
publishes both the results and the raw data behind them. Every figure quoted in this repository
comes from a deployment in Sweden Central; none is estimated or extrapolated.

## Summary of findings

Run a thin, stateless, self-hosted layer for intent routing, authorization and private network
access. Delegate the agent runtime — session state, sandboxing, scaling, identity and telemetry —
to Foundry Hosted Agents, and pre-create sessions to offset the cold-start cost of doing so.

The supporting evidence is set out in [Results](#results), and the decision procedure that follows
from it in [docs/05-choosing-a-hosting-model.md](docs/05-choosing-a-hosting-model.md).

## Method

Hosting comparisons are usually presented as feature matrices. A feature matrix compares
documentation rather than observed behaviour, which limits how much confidence it can carry.

This lab takes a different approach. A single agent — a trip planner with two tools — is deployed
four ways **without modification**. All four variants import the same `agent_core` package, so any
difference in outcome is attributable to the hosting model rather than to the agent code.

## The four variants

| Variant | Description | Platform |
|---|---|---|
| `selfhosted-naive` | FastAPI and the agent, 2 replicas, state on local disk | Azure Container Apps |
| `selfhosted-hardened` | As above, with state in Cosmos DB behind a private endpoint | Azure Container Apps + VNet |
| `hosted-agent` | The agent deployed to the Foundry managed runtime | Foundry Hosted Agents |
| `hybrid-router` | Stateless self-hosted router in front of the hosted agent | Container Apps to Foundry |

`selfhosted-naive` is a realistic configuration rather than a deliberately weak one. Two replicas
is a reasonable default, and persisting session state to local disk is a common first
implementation. The defect arises from the combination of the two, which is why it survives code
review and reaches production.

## Results

### 1. Session state across replicas

Three turns are sent in sequence: *"Add Paris to my trip"*, *"Add Rome"*, *"What is in my trip?"*

| Variant | Result | Distinct replicas | Response to turn 3 |
|---|---|---|---|
| `selfhosted-naive` | **Fail** | 2 | *"Your trip currently includes: Paris."* |
| `selfhosted-hardened` | Pass | 2 | *"Your trip is: Paris -> Rome"* |
| `hosted-agent` | Pass | n/a (platform-managed) | *"Your trip is: Paris -> Rome."* |
| `hybrid-router` | Pass | 2 | *"Your trip includes: Paris -> Rome"* |

Quoted from `01_session_state_20260826T110926Z.json`. Response wording is model output and varies
between runs; the verdict and replica counts do not.

The failure mode in `selfhosted-naive` warrants attention because of how it presents. The agent
did not crash, time out, or return an error status. It answered the question fluently, promptly
and incorrectly. No health probe, alerting rule or integration test asserting HTTP 200 would
detect it.

This is the characteristic risk introduced by self-hosting: not unavailability, but confident
incorrect answers.

The defect and its resolution use the same class. `agent_core.state.DiskStore` is a defect in
`selfhosted-naive` and correct in `hosted-agent` — identical code with opposite guarantees,
because the platform beneath it differs. See
[docs/02-state-and-sessions.md](docs/02-state-and-sessions.md).

### 2. Cold start

Four rounds, median values.

| Variant | First turn | Warm turn | Penalty |
|---|---|---|---|
| `selfhosted-naive` | 1884 ms | 2048 ms | −164 ms |
| `selfhosted-hardened` | 2164 ms | 1939 ms | +225 ms |
| `hosted-agent` | **10218 ms** | 2400 ms | **+7818 ms** |
| `hybrid-router` | **10004 ms** | 2413 ms | **+7592 ms** |

This is the principal cost of the hosted model. Establishing a new session requires the platform
to provision a sandbox, which takes seven to eight seconds. Self-hosted containers held warm by a
minimum replica count do not incur it.

The experiment was repeated four times, hours apart and across a security refactor of the session
path. The hosted penalty measured +6840, +7818, +6801 and +8985 ms, while the self-hosted controls
ranged between −379 and +380 ms. The effect is approximately twenty times the run-to-run variance,
so the conclusion does not depend on the run selected.

The second column is equally significant. Once the session exists, the two models are within
roughly 400 ms of each other. The penalty is incurred **per session rather than per turn**, and it
is paid when the session is created — which need not coincide with the user's first message.

One qualification with direct cost implications: the per-session characteristic holds only if the
client returns the session handle and `previous_response_id` on every turn. If they are omitted,
each turn provisions a new sandbox — 9634 ms per turn instead of 2413 ms — and no error is raised
to indicate it. This was observed in an early version of the test harness before it was corrected.

### 3. Session pre-creation

Provisioning the sandbox when the chat window opens, then sending the user's first message with
`agent_session_id` **in the request body**:

| Attachment mechanism | Session create | First user turn | Session reused |
|---|---|---|---|
| Body field `agent_session_id` | 6558 ms | **3174 ms** | **100%** (4/4) |
| Header `x-agent-session-id` | 6174 ms | 9458 ms | **0%** (0/4) |

First-turn latency falls from 9.5 s to 3.2 s, with the 6.5 s of provisioning occurring while the
user reads the greeting.

The second row is the reason this is recorded as a finding rather than an optimisation. Supplying
the pre-created identifier in the `x-agent-session-id` **header** reattached zero times out of
four: the header is honoured only alongside `previous_response_id`, which by definition does not
exist on the first turn — precisely the turn pre-warming targets. The request succeeds, the
response is correct, and the provisioned session is discarded without notice.

Verified end to end through the deployed router: `POST /prewarm` completed in 9728 ms, and the
user's first message returned in **3968 ms** with the session correctly reattached.

Priming with a throwaway message achieves a comparable result (approximately 70 percent saved) at
the cost of an additional model call. Self-hosted variants were measured as controls and did not
move. See [docs/04-cold-start-and-scaling.md](docs/04-cold-start-and-scaling.md).

### 4. Deployment surface

Lines of code owned and maintained by the implementing team.

| | Lines | Ratio to agent logic |
|---|---|---|
| Shared agent logic | 122 | 1.0x |
| Self-hosted only | **527** | 4.32x |
| Hosted only | **53** | 0.43x |

Self-hosting requires 9.94 times the supporting code to operate the same agent.

The 527 lines comprise a web server (84), a Dockerfile (25), pinned requirements (9), Container
Apps Bicep (273), a virtual network (96) and a VNet-injected environment (40). Each line is
authored, reviewed, patched and operated by the team.

The hosted variant's 53 lines comprise a 20-line handler, four requirements and a 29-line
`azure.yaml`. See [docs/03-the-cost-of-self-hosting.md](docs/03-the-cost-of-self-hosting.md).

### 5. Session isolation

The hosted runtime allocates each session its own sandbox. The experiment tested what prevents a
third party from opening that sandbox. The answer is knowledge of the session identifier alone.

```
turn A   agent_session_id=X, "Add Barcelona to my trip"   -> "Added Barcelona to your trip."
turn B   agent_session_id=X, NEW conversation,
         no previous_response_id, "What is in my trip?"   -> "Your trip includes: Barcelona."
control  agent_session_id=Y, same question                -> "Your trip is empty."
```

Turn B shares nothing with turn A except the identifier. The runtime also accepts identifiers it
never issued (64 repetitions of the character `a` is sufficient) and echoes back whichever
identifier it receives, so a failed reattachment cannot be detected from the response — only from
latency.

This is not a defect in the runtime. Every call above used the same Azure AD identity, and where
each end user calls with their own credentials, Entra has already established separation.

The consideration applies specifically to the hybrid model: the router calls the runtime with a
single managed identity on behalf of every user, so the runtime cannot distinguish between them.
Passing a client-supplied session identifier through unmodified — as the initial implementation
did — allows any user who obtains another's identifier to read that session.

The mitigation is a signed handle binding a session to the user it was issued to. Measured cost:
**34 lines**, no database, and the router remains stateless. Verified against the deployment:

| Scenario | Result |
|---|---|
| Owner replays their own handle | **200**, own state returned |
| Second user replays that handle | **403** |
| Second user supplies a raw session identifier | **403** |

See [docs/02-state-and-sessions.md](docs/02-state-and-sessions.md#finding-4-the-session-id-is-the-isolation-boundary-and-that-changes-what-the-router-owes-you).

### 6. Authorization-aware routing

Session isolation answers *"is this your conversation?"*. It does not answer *"are you allowed to
reach this agent at all?"* — and that second question is the one the requirement asks for.

Azure RBAC does not answer it either. In the hybrid pattern the router calls the runtime with its
own managed identity, so by the time a request reaches Azure there is exactly one principal — the
router — whichever end user started it. Every distinction between users has already been erased.
Whatever the router forwards, Azure authorizes.

So the decision has to be made in the router, before the call. Measured against the deployment,
with one agent open to all callers and a second restricted to a named group:

| Message | Groups presented | Result |
|---|---|---|
| ordinary trip | *(none)* | allowed → `trip-planner` |
| corporate travel | `travel-admin` | allowed → `trip-planner-corporate` |
| corporate travel | `engineering` | **403** |
| corporate travel | *(none)* | **403** |

4/4, from `07_authorization_routing_20260826T141209Z.json`. The deny cases never reached the
runtime: they were refused before a token was acquired or a session opened.

The fourth row is the one worth keeping. A caller presenting no groups is refused a restricted
agent, because an unstated entitlement is not a granted one. The inverse — treating a missing
field as a wildcard — is how an entitlement check comes to pass for everybody who omits it.

This experiment exists because reviewing the router exposed a gap in our evidence rather than in
the code. The check was correct, but every deployed agent had been entitled to `*`, so its deny
branch had never once executed against a live deployment. Adding a restricted route made both
branches reachable. A control that has never refused anything is an assertion, not a control.

## Governance constraints observed

During construction of the lab, `selfhosted-hardened` began returning HTTP 500:

```
CosmosHttpResponseError (Forbidden) Request originated from IP ... blocked by
your Cosmos DB account firewall settings
```

The account had `publicNetworkAccess: Disabled`. Re-enabling it through Bicep, then through
`az cosmosdb update`, then through `az rest PATCH`, each appeared to succeed and each was reverted
within seconds. The cause was an Azure Policy assignment (`CosmosDB_PublicNetwork_Modify`, effect
`modify`) rewriting the property on every write. A second policy in the same assignment,
`CosmosDB_LocalAuth_Modify`, had already disabled key-based authentication, so the implementation
additionally required `DefaultAzureCredential` and a Cosmos data-plane role assignment in place of
a connection string.

IP allow-listing was not a viable alternative: the Container Apps environment has more than 160
outbound IP addresses, none of them stable.

The self-hosted variant therefore required a virtual network, a subnet, a private endpoint, a
private DNS zone, a VNet link and a workload-profiles environment. Because a Container Apps
environment's VNet assignment is immutable, both applications had to be deleted and recreated.

The Foundry hosted agent required none of these. It reached the platform's own data plane and was
not subject to the policy.

This is the cost a feature matrix does not capture: in a governed enterprise tenant, the cost of
self-hosting is determined less by the code written than by the organisational controls the team
becomes responsible for satisfying.

## Recommendation

A hybrid deployment — not as a compromise, but because the two models fail in different places and
the hybrid is the configuration in which neither failure falls to the implementing team.

```
    client
      |
      v
  +-------------------------------------+
  |  Router  (self-hosted, stateless)   |   Owned by the team. 175 lines.
  |  - intent classification            |   Scales freely: holds no session state.
  |  - authorization / entitlements     |   Issues signed session handles, which
  |  - signed session handles           |   separate users once the runtime is
  |  - private network egress           |   called with a single identity.
  +---------------+---------------------+
                  |  agent_session_id + previous_response_id
                  v
  +-------------------------------------+
  |  Foundry Hosted Agent runtime       |   Owned by the platform.
  |  - per-session sandbox and state    |   No Dockerfile, no VNet,
  |  - scaling, identity, telemetry     |   no state backend, no policy exceptions.
  +-------------------------------------+
```

The router is self-hosted because it performs functions the platform does not model: selecting
which agent handles a request, and applying authorization before routing. These are substantive
reasons to operate compute, and none of them concerns running a model.

The runtime is hosted because session state, sandboxing, scaling, identity and telemetry are
solved problems, and reimplementing them costs 9.94 times the code and an additional network
topology.

The router holds no session state. It returns both session handles to the caller and accepts them
on the following turn, which is why it tolerates being distributed across two replicas in the
experiment above while still passing the state test.

The hybrid does introduce one obligation. Because the router calls the runtime with a single
managed identity, session authorization becomes the team's responsibility rather than the
platform's — 34 lines of signed handles, and a security review scoped to look for it. The cost is
modest, and more easily planned for than discovered.

| Requirement | Self-hosted | Hosted | Hybrid |
|---|---|---|---|
| Session state across replicas | Team-implemented | Structural | Structural |
| Session isolation between users | Team-implemented | Entra, per-caller | **Team-implemented** (34 lines) |
| Authorization-aware routing | Yes | No | Yes (measured: 4/4, `07`) |
| Private network / on-premises backends | Yes | No | Yes |
| Arbitrary Python business logic | Yes | Yes | Yes |
| Custom guardrails before dispatch | Yes | Partial | Yes |
| Scales without a state backend | No | Yes | Yes |
| First-turn latency | ~1.9 s | ~10.2 s | ~10.0 s |
| First-turn latency, session pre-created | ~1.9 s | ~3.2 s | ~4.0 s |
| Lines of code maintained | 527 | 53 | 228 (53 + 175) |

## Repository structure

```
docs/           analysis, one argument per file
runbooks/       step-by-step deployment procedures
src/agent_core/ the agent, shared unmodified by all four variants
src/selfhosted/ FastAPI server and hybrid router
src/hosted/     the Foundry hosted handler
infra/          Bicep: platform, apps, network, VNet environment
experiments/    the measurements and their raw JSON results
                (results/README.md maps every published figure to its run)
scripts/        deployment and preparation helpers
```

### Documents

1. [The four variants](docs/01-the-four-variants.md) — what each variant is, and the basis for comparison
2. [State and sessions](docs/02-state-and-sessions.md) — the same class as defect and as feature; the two session identifiers; where user isolation originates
3. [The cost of self-hosting](docs/03-the-cost-of-self-hosting.md) — the 9.94x figure, and the governance policy accounting for most of it
4. [Cold start and scaling](docs/04-cold-start-and-scaling.md) — the 7.8 s penalty and how to eliminate it
5. [Choosing a hosting model](docs/05-choosing-a-hosting-model.md) — a decision procedure

### Runbooks

1. [Deploy a hosted agent](runbooks/01-deploy-hosted-agent.md) — including the behaviour of the hosted contract
2. [Deploy a self-hosted agent](runbooks/02-deploy-self-hosted-agent.md)
3. [Deploy the hybrid router](runbooks/03-deploy-hybrid-router.md)
4. [Troubleshooting](runbooks/04-troubleshooting.md) — every error encountered, with its cause
5. [Teardown](runbooks/05-teardown.md)

## Reproducing the results

Prerequisites: Azure CLI (authenticated), `azd` 1.20 or later, Python 3.11 or later, and an Azure
subscription.

```powershell
git clone https://github.com/chapi-dev/agent-hosting-lab.git
cd agent-hosting-lab
./scripts/deploy.ps1            # provisions all resources, writes .env.lab
./scripts/run-experiments.ps1   # runs all seven experiments
```

`deploy.ps1` is idempotent and takes approximately 25 minutes end to end, most of which is spent
waiting for Foundry and the Container Apps environment. Results are written to
`experiments/results/` as timestamped JSON.

The lab provisions billable resources. See [runbooks/05-teardown.md](runbooks/05-teardown.md) for
removal instructions and the cost of leaving it idle.

## Scope and limitations

The lab measures **hosting** specifically. The agent is intentionally minimal — two tools, one
model call per turn, and a deterministic classifier in the router — which keeps the experiments
repeatable and prevents agent quality from confounding the hosting measurements.

Outside the scope of this lab: multi-agent orchestration at depth, evaluation pipelines, prompt
quality, and cost at scale. These are significant concerns, and largely orthogonal to the hosting
decision.

Results reflect the platform as observed in Sweden Central during August 2026. Managed platforms
change; re-running the experiments is the intended way to confirm that the findings still hold.

## Test environment

Sweden Central. A Foundry project with `gpt-5.4-mini`, Log Analytics and Application Insights, a
user-assigned managed identity, Azure Container Registry, serverless Cosmos DB, and a
VNet-injected Container Apps environment on workload profiles. All infrastructure is defined in
`infra/`.

## Disclaimer

This is an independent engineering lab, not an official Microsoft product, and carries no support
commitment. It records the behaviour of Azure services as measured on the dates stated.

## License

Released under the MIT License. See [LICENSE](LICENSE).