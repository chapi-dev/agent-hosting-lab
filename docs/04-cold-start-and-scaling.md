# 4. Cold start and scaling

This document is about the one thing hosted agents are genuinely worse at, and what to do
about it.

## The measurement

`experiments/02_cold_start.py`, 4 rounds, medians:

| Variant | First turn | Warm turn | Penalty |
|---|---|---|---|
| `selfhosted-naive` | 1884 ms | 2048 ms | −164 ms |
| `selfhosted-hardened` | 2164 ms | 1939 ms | +225 ms |
| `hosted-agent` | **10218 ms** | 2400 ms | **+7818 ms** |
| `hybrid-router` | **10004 ms** | 2413 ms | **+7592 ms** |

Ten seconds to a first answer is bad. In a chat UI it is the difference between "thinking" and
"broken", and users will reload the page.

The self-hosted variants show essentially no penalty because they run `minReplicas: 2`. That is
not free — it means paying for two containers 24 hours a day whether anyone is chatting or not.
The asymmetry is the trade, not a flaw in the measurement.

The hybrid inherits the hosted penalty exactly, which is expected: the router adds about
200 ms of proxying and the sandbox provisioning happens downstream regardless.

We ran this experiment three times over the course of the lab. The hosted penalty came out at
+6840 ms, +7818 ms and +6801 ms; the self-hosted controls at +174, −164, +380 and −289, +225,
−379 ms. The effect is an order of magnitude larger than the run-to-run spread, so the
conclusion does not depend on which run you read. Raw files are in `experiments/results/`.

## The shape of the problem

Look at the second column. Once the session exists, hosted (2400 ms) and self-hosted (1939 ms)
are within about 460 ms — and much of that is the extra network hop and token acquisition, not
the runtime.

So the penalty is **per session, not per turn**. A twenty-turn conversation pays it once. And
crucially, it is paid **when the session is created**, which is not necessarily when the user
sends their first message.

Those are two different moments. In any real chat interface they are separated by the time the
user spends reading the greeting and typing. That gap is usually five to fifteen seconds, and
it is time the user was going to spend anyway.

### The failure mode that turns "per session" into "per turn"

"Per session, not per turn" is only true if the client actually keeps the session. The first
version of our own experiment did not, and the result is worth showing because it is the
easiest mistake to make in a real integration:

| | Turn 1 | Turn 2 | Turn 3 |
|---|---|---|---|
| Handles echoed back | 10004 ms | 2413 ms | 2413 ms |
| Handles dropped | 9115 ms | 9634 ms | 9634 ms |

Dropping the handles does not raise an error and does not corrupt the answer. The agent replies
correctly every time, from a brand-new sandbox, and the conversation quietly costs a cold start
per turn — 193 s instead of 56 s over a twenty-turn chat, a 3.5x tax for a missing field. The
only symptom is that latency never improves after the first message.

If your hosted-agent latency is flat at around nine seconds instead of dropping to two, this is
almost certainly why. See `time_router()` in `experiments/02_cold_start.py` for the correct
propagation loop, and `docs/02-state-and-sessions.md` for which handle does what.

## The fix: create the session before the user finishes typing

Create the session the moment the chat window opens, not when the user hits send. There are two
ways to do it, and the lab measured both.

### Option A — pre-create the session explicitly (recommended)

The runtime exposes an API that provisions a sandbox without running the agent:

```http
POST {agent-endpoint}/sessions?api-version=v1
{}
```
```json
{ "agent_session_id": "...", "status": "active", "expires_at": 1790306508 }
```

Then send the user's first real message with that id **in the request body**.

`experiments/05_session_precreate.py`, 4 rounds, medians:

| Attachment mechanism | Session create | First real turn | Session reused |
|---|---|---|---|
| **body field `agent_session_id`** | 6558 ms | **3174 ms** | **100%** |
| header `x-agent-session-id` | 6174 ms | 9458 ms | **0%** |

**The first turn drops from ~9.5 s to ~3.2 s** — a 66% reduction — and the 6.5 s of provisioning
happens while the user is still reading the greeting.

The second row is the trap. Passing the pre-created id in the `x-agent-session-id` header
reattached **zero times out of four**. The header is only honoured alongside
`previous_response_id`, which does not exist on turn one — the exact turn pre-warming targets.
The request succeeds, the answer is correct, and the pre-created session is silently discarded.
See [02-state-and-sessions.md](02-state-and-sessions.md).

This option costs no model call and leaves no junk in the conversation history.

Re-run independently at 3 rounds, the split was identical: body 3458 ms and 100% reuse, header
8776 ms and 0% reuse. Two runs, seven attempts per mechanism, zero header reattachments.

### Option B — prime with a throwaway message

If you cannot call the sessions API, send a discarded message like `"hello"` when the window
opens. `experiments/04_prewarm.py`, 8 rounds, 8-second simulated think time, medians:

| Variant | Cold | Pre-warmed | Saved |
|---|---|---|---|
| `selfhosted-naive` | 1968 ms | 2155 ms | −187 ms (−9.5%) |
| `selfhosted-hardened` | 2476 ms | 2228 ms | 248 ms (10.0%) |
| **`hosted-agent`** | **9090 ms** | **2794 ms** | **6296 ms (69.3%)** |
| **`hybrid-router`** | **8982 ms** | **2636 ms** | **6346 ms (70.7%)** |

**About 70% of the user-visible first-turn latency disappears.** Nine seconds becomes 2.8
seconds — indistinguishable from the self-hosted steady state.

Two details make this trustworthy rather than convenient:

1. **The self-hosted rows are a control and they did not move.** −9.5% and +10.0% straddle zero,
   which is what should happen when there is nothing to pre-warm. If they had also improved, the
   experiment would be measuring something other than sandbox provisioning.
2. **The sandbox survived an 8-second idle gap.** The priming call and the real question are
   separated by a full sleep, and both state and session persisted. Pre-warming that worked only
   for instantaneous follow-ups would be useless, because real users pause.

Run it with fewer rounds and the controls get noisy: at 3 rounds one control read −64% and
another +57%, because a single slow model call moves a 3-sample median. The hosted and hybrid
rows stayed in the 57–73% band at every sample size we tried. Eight rounds is the smallest n
where the controls settle; that is the run quoted above
(`experiments/results/04_prewarm_20260826T042647Z.json`).

The priming calls themselves took 6.9–11.5 seconds. The cold start did not disappear; it moved
to a moment where nobody is waiting for it.

The cost of Option B is one wasted model call per conversation and a junk turn in the history.
Prefer Option A.

### Implementing it

`src/selfhosted/router.py` exposes this as `POST /prewarm`. Verified end to end through the
deployed router: pre-warm took 9728 ms (hidden), and the user's first real message returned in
**3968 ms** with the session correctly reattached.

```
User opens chat
      │
      ├──▶ POST /prewarm            (fire and forget, no spinner)
      │        │
      │        └──▶ POST {endpoint}/sessions   ~6-7 s
      │
User reads greeting, types            ~5-15 s
      │
      └──▶ POST /chat  { message, agent_session_id } ────▶ ~3.2 s ✓
```

Practical notes:

- Do not block UI rendering on the pre-warm call, and do not show its result.
- Put `agent_session_id` in the **body**. This is the single most common way to get no benefit.
- From turn two, send `previous_response_id` as well so the conversation continues.
- If pre-warming fails, omit `agent_session_id` entirely. The runtime allocates a sandbox on
  demand, so the conversation still works — just slower on turn one. Pre-warming is an
  optimisation, never a dependency.
- If the user sends a message before pre-warming completes, wait for the returned id. It arrives
  in under ten seconds and the experience is no worse than the cold path.
- Set a client-side timeout of 60 s or more on the first call. The 30 s default in several HTTP
  clients is uncomfortably close to a bad-day cold start.
- Sessions expire after 30 days, so there is no need to refresh them aggressively.

### When pre-warming does not apply

- **Server-to-server / batch.** Nobody is watching, so eight seconds is irrelevant.
- **Proactive notifications** (an agent starting the conversation). There is no user typing, but
  there is also no user waiting — do the work before you notify.
- **Genuine sub-second first-turn requirements.** If your SLA is "first token in under a second
  from a cold session", hosted agents are the wrong choice and this document will not persuade
  you otherwise.

## Scaling

The two models scale along different axes, and the difference matters more than the latency.

### Self-hosted

You scale replicas. Each replica is a general-purpose worker handling any session, which is
precisely why state cannot live on it and why `selfhosted-naive` fails.

What you configure and own:

- Scale rules (HTTP concurrency, CPU, KEDA triggers)
- `minReplicas` — the warm-latency-versus-cost dial
- `maxReplicas` — the blast-radius-versus-throughput dial
- Per-replica CPU and memory
- The state backend's own scaling: Cosmos RU/s or serverless limits, connection pooling,
  partition-key hot-spotting

The subtle one is the last. Every user's state lives in a shared store, so the store becomes a
throughput bottleneck *and* a single point of failure for every conversation at once. This lab's
Cosmos account is serverless, which sidesteps capacity planning at lab scale and would not at
"thousands of users".

### Hosted

You scale sessions. Each session gets its own sandbox, and the platform decides how many exist.

What you configure: container CPU and memory per session, in `azure.yaml`.

That is the entire list. There is no `minReplicas`, no scale rule, no shared state store, and no
partition key — because there is nothing shared to contend on. Sessions are isolated by
construction, so a hot session cannot slow another one down.

### The honest comparison at scale

| | Self-hosted | Hosted |
|---|---|---|
| Unit of scale | replica | session |
| State contention | shared store, tunable and breakable | none |
| Idle cost | `minReplicas` × always-on | none |
| Burst behaviour | scale-up lag, then queueing | per-session provisioning (~8–10 s) |
| Noisy neighbour | yes, via the shared store | no |
| Capacity planning | RU/s, replicas, connections | model quota only |
| Failure blast radius | all sessions on the affected replica or store | one session |

For an employee-facing assistant with thousands of users and bursty usage — everyone opening the
HR bot on the first of the month — the hosted model's isolation is worth more than the
self-hosted model's warm start. Bursts are precisely when a shared state store and a scale-up
lag hurt most, and precisely when per-session isolation costs nothing.

## Recommendation

Use hosted or hybrid, and pre-create sessions. It is a handful of lines in the client and it
recovers two thirds of the only latency disadvantage hosted agents have.

Do not "solve" cold start by keeping self-hosted replicas warm unless you have measured that you
need it. That choice buys ~460 ms of steady-state latency and costs you the 527 lines, the
network topology, the shared-state bottleneck, and the session-isolation security boundary
described in [03-the-cost-of-self-hosting.md](03-the-cost-of-self-hosting.md).
