# 1. The four variants

The value of this lab depends entirely on one property: **the agent is identical in all four
deployments.** If it were not, every measured difference could be explained away as a coding
difference rather than a hosting difference.

So it is worth being precise about what is shared and what is not.

## What is shared

`src/agent_core/` — 122 lines across three files — is imported verbatim by every variant:

- `agent.py` — the instructions, the two tools (`add_city`, `show_itinerary`), the chat client
  factory, and the agent factory.
- `state.py` — two interchangeable state backends, `DiskStore` and `CosmosStore`.
- `__init__.py` — the exports.

Every variant calls the same `build_agent(store, session_id, client=...)`. Every variant talks
to the same Foundry project and the same `gpt-5.4-mini` deployment. The system prompt is byte
for byte the same string.

There is one detail worth calling out, because it removes a whole class of doubt from the
experiments. `build_tools()` closes over the session id:

```python
def build_tools(store, session_id):
    def add_city(city: str) -> str:
        itinerary = store.load(session_id)
        ...
```

The model never sees a session id and cannot pass the wrong one. When state goes missing in an
experiment, the cause is provably the hosting layer, not the model choosing badly.

## What differs

Only the code *around* the agent.

### `selfhosted-naive`

`src/selfhosted/server.py` on Azure Container Apps. FastAPI, a `/chat` endpoint, a `/health`
probe, manual OpenTelemetry configuration, and `DiskStore` writing to the container filesystem.

Configured with `minReplicas: 2`. Every response reports which replica served it — that field
is how the failure becomes visible rather than merely suspected.

Nothing about this configuration is unusual. Two replicas is what you set when you want
availability. Local disk is where state goes when you first make an agent remember something.
The bug is in the combination of the two, which is exactly why it ships.

### `selfhosted-hardened`

The same container image and the same server code, with `STATE_BACKEND=cosmos`. `CosmosStore`
replaces `DiskStore`: shared state, correct across replicas.

This variant is what a competent team ships *after* being bitten once. It is the honest
comparison point for hosted agents — not the naive version.

It also carries the full weight of what that correctness costs: a Cosmos account, a partition
key strategy, TTL configuration, 404 handling, a managed identity with a data-plane role
assignment, a virtual network, a private endpoint, a private DNS zone, and a Container Apps
environment on workload profiles. See [03-the-cost-of-self-hosting.md](03-the-cost-of-self-hosting.md).

### `hosted-agent`

`src/hosted/main.py` deployed to the Foundry managed runtime with
`azd ai agent init --deploy-mode code --dep-resolution remote_build`.

Twenty effective lines. No web framework, no Dockerfile, no health probe, no telemetry setup,
no state backend, no network. The platform supplies the Responses HTTP surface, builds the
image, injects `APPLICATIONINSIGHTS_CONNECTION_STRING`, and gives every session its own sandbox.

It imports `DiskStore` — the same class that fails in `selfhosted-naive` — and it is correct
here. That is not an accident of the lab; it is the whole point, and
[02-state-and-sessions.md](02-state-and-sessions.md) is about why.

### `hybrid-router`

`src/selfhosted/router.py` on Container Apps, in front of `hosted-agent`.

175 lines. It classifies intent, checks entitlements, forwards to the hosted runtime, and
returns both session handles to the caller. It holds **no session state**, which is why it
scales to any number of replicas without a database — and why it passes the state experiment
while being served by two different replicas across three turns.

## Why `selfhosted-naive` is not a straw man

It would be easy to build a deliberately broken self-hosted variant, watch it fail, and declare
hosted agents the winner. That would prove nothing.

`selfhosted-naive` is included because the failure it demonstrates is *quiet*. Consider what a
reasonable team's checks would say about it:

- Health probe: **passing**. The container is healthy; only the data is wrong.
- Integration test asserting HTTP 200: **passing**.
- Single-replica local testing: **passing**. The bug requires more than one replica.
- Single-replica staging: **passing**, for the same reason.
- Load test measuring latency and error rate: **passing**. Zero errors.
- Production with two replicas and real users: **silently wrong, roughly half the time.**

Every gate a normal delivery pipeline has is green. The bug reaches users because the failure
mode is a correct-looking sentence, and correct-looking sentences are what an agent produces
by design.

`selfhosted-hardened` exists so that the comparison is not against this failure but against a
team that already fixed it — and so the cost of the fix can be counted honestly.

## The comparison the lab actually makes

| | naive | hardened | hosted | hybrid |
|---|---|---|---|---|
| Agent logic | shared | shared | shared | shared |
| Model | `gpt-5.4-mini` | same | same | same |
| Foundry project | shared | shared | shared | shared |
| State backend | disk (per replica) | Cosmos | sandbox `$HOME` | delegated |
| Replicas | 2 | 2 | platform | 2 |
| Code you own | 527 lines | 527 lines | 53 lines | 228 lines |
| Network required | no | **VNet + PE** | no | no |

Read across the top three rows: everything that could bias the result is held constant.
Read the bottom four: everything that differs is infrastructure.

That is the design, and it is what makes the numbers in the README worth anything.
