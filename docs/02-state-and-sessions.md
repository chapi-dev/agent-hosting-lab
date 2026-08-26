# 2. State and sessions

This is the document that matters most. Two findings, both discovered the hard way, both
capable of costing a team a week each.

---

## Finding 1: the same class is a bug and a feature

`agent_core.state.DiskStore` is about forty lines. It writes a JSON file per session id and
reads it back. There is nothing clever in it and nothing wrong with it.

`src/selfhosted/server.py` imports it. `src/hosted/main.py` imports it. Same import, same
class, no subclassing, no configuration difference.

In `selfhosted-naive` it loses user data. In `hosted-agent` it is correct.

### What happened in the self-hosted variant

Three turns against `selfhosted-naive`, from `experiments/results/01_session_state_*.json`:

| Turn | Reply | Replica |
|---|---|---|
| "Add Paris to my trip" | *"Added Paris."* | `...-kh7gp` |
| "Add Rome" | *"Added Rome."* | `...-fgnmx` |
| "What is in my trip?" | *"Your trip currently includes: **Paris**."* | `...-kh7gp` |

Rome was added to a file on the filesystem of replica `fgnmx`. Turn 3 was load-balanced back
to `kh7gp`, which had never heard of Rome and answered from its own copy.

Note that the agent said *"Added Rome"* on turn 2 and meant it. The write succeeded. The
container was healthy. The tool returned successfully. The state simply belonged to a machine
that the next request did not reach.

### Why the same class is correct in the hosted variant

The Foundry runtime gives every session **its own sandbox** — its own compute, its own
filesystem — and persists `$HOME` between turns and across idle periods. There is no second
replica to route to, because the session *is* the unit of isolation.

So `DiskStore` on a per-session durable filesystem is not a workaround. It is the natural
correct thing.

### The general lesson

> Correctness of state-handling code is not a property of the code. It is a property of the
> code *plus* the platform underneath it.

A code review cannot catch this. Both files pass review. A test cannot catch it unless the test
deliberately runs multiple replicas and asserts on conversational continuity — which is a test
most teams write only after the incident.

Self-hosting means every developer touching the agent must hold the replica model in their
head, forever. Hosted means the question does not arise.

## Why the naive failure is worse than an outage

An outage is loud. Someone gets paged, a dashboard turns red, users retry.

This failure produces a fluent, immediate, confident, wrong answer. Applied to the use cases
this pattern is usually deployed for:

- An HR assistant that "forgets" the employee's legal entity mid-conversation and then answers
  a leave-policy question using the default entity's rules.
- A learning recommender that loses the user's completed-training context and recommends a
  course they finished last year.
- A ticket-creation flow that loses an uploaded attachment reference between turns and files
  the ticket without it.

None of those page anyone. All of them erode trust, and the failure is invisible in aggregate
metrics because error rate stays at zero.

---

## Finding 2: there are two ways to attach a session, and only one of them always works

This one cost the lab several hours and would cost a production team more, because the failure
looks exactly like the failure above while having a completely different cause.

The Responses protocol as used by Foundry Hosted Agents carries **two independent handles**:

| Handle | Where it goes | What it controls |
|---|---|---|
| `previous_response_id` | request **body** | continues the **conversation** (message history) |
| `agent_session_id` | request **body** | selects the **sandbox** (the agent's `$HOME`) |
| `x-agent-session-id` | request **header** | also selects the sandbox — but conditionally |

They are not synonyms and neither implies the other. Session and conversation are separate
concepts: one is *where the agent's files live*, the other is *what the model remembers*.

### The header is not equivalent to the body field

`experiments/05_session_precreate.py` runs both mechanisms against explicitly pre-created
sessions, 4 rounds each:

| Attachment mechanism | Session reused | First turn |
|---|---|---|
| **body field `agent_session_id`** | **100%** (4/4) | **3174 ms** |
| header `x-agent-session-id` | **0%** (0/4) | 9458 ms |

The header is honoured **only alongside `previous_response_id`**. On turn one there is no
previous response by definition — so the header is ignored, the runtime silently allocates a
fresh sandbox, and you pay a full cold start on a session you had already provisioned.

Nothing errors. You get a correct answer, slowly, from a sandbox you did not intend to use.

**Use the body field.** It has no dependency on conversation state and works on every turn
including the first.

### The failure modes, in full

- **Only `previous_response_id`**: the model sees the conversation history, but the agent gets a
  **fresh sandbox**. Anything written to disk last turn is gone. The model answers confidently
  from message history and is subtly wrong about anything held in state.
- **Only `x-agent-session-id`** (header): ignored. New sandbox every turn.
- **Only `agent_session_id`** (body): sandbox reattached. Conversation history not continued —
  fine if all your state is in the sandbox, wrong if you rely on the model remembering.
- **`agent_session_id` + `previous_response_id`** (both in the body): sandbox reattached *and*
  conversation continued. **This is the correct combination.**

The response returns the session id in two places, and it is worth reading both defensively:

```
X-Agent-Session-Id: <header>
body.agent_session_id: <same value>
```

### How to get this right

`src/selfhosted/router.py` shows the pattern. The router returns both handles on every reply
and takes them back on the next request:

```python
payload = {"input": message, "store": True}
if agent_session_id:
    payload["agent_session_id"] = agent_session_id       # body, not header
if previous_response_id:
    payload["previous_response_id"] = previous_response_id
```

The client echoes both back. The router itself stores nothing — which is precisely why it can
run on any number of replicas without a database. The state lives in the hosted runtime; the
*handles* live with the client.

If you would rather not push handles to the client, store them server-side keyed by your own
session id. But note what you have then reintroduced: a shared store, a partition strategy, a
TTL policy and an availability dependency. That is the self-hosted cost creeping back in
through the side door. Passing them through is cheaper and scales better.

### Sessions can be created explicitly

You do not have to wait for a message to create a sandbox:

```http
POST {agent-endpoint}/sessions?api-version=v1
{}
```
```json
{ "agent_session_id": "...", "status": "active", "expires_at": 1790306508 }
```

`azd ai agent sessions create <agent>` is the CLI wrapper. Sessions expire after **30 days** by
default.

Note the URL: sessions hang off the *agent endpoint root*, not off the protocol path.
`.../endpoint/sessions` works; `.../endpoint/protocols/openai/sessions` returns 404.

This is the basis of the pre-warming technique in
[04-cold-start-and-scaling.md](04-cold-start-and-scaling.md) — and the reason the body-versus-header
distinction matters so much, since pre-creation is worthless if you cannot attach to the result.

---

## Finding 3: `conversation_chain_id` is not stable, and a constant key is the right answer

Inside the hosted handler, keying state by session looks like the careful thing to do:

```python
agent = build_agent(STORE, context.conversation_chain_id)   # WRONG
```

It is wrong. `context.conversation_chain_id` is **not stable across turns**. Each turn wrote a
different file, and by turn three the agent answered *"Your trip is empty"* after two
successful adds — again with no error of any kind.

The correct version looks careless and is not:

```python
agent = build_agent(STORE, "session")   # correct
```

The sandbox is *already* per-session. The platform has given this conversation its own machine
and its own `$HOME`, and no other conversation can reach either. There is nothing left to key
by. A constant path inside a private sandbox is isolated by construction.

Compare the blast radius of getting the key wrong in each model:

- **Self-hosted:** the session id is load-bearing. Get it wrong and one user can read another
  user's data. It is a security incident.
- **Hosted:** get it wrong and one conversation loses its own state. It is a bug.

The isolation is structural rather than something the code must get right. That difference —
not the line count — is the strongest argument for hosted runtimes in a multi-tenant,
employee-facing deployment.

---

## Practical checklist

If you self-host:

- [ ] Assume more than one replica from day one, including in staging.
- [ ] Never store conversational state on local disk, even "temporarily".
- [ ] Write one test that runs ≥2 replicas and asserts continuity across turns. It is the only
      test that catches this class of bug.
- [ ] Treat the session id as a security boundary; review it like one.
- [ ] Give the state store a TTL, or you will be storing conversations forever.

If you use hosted agents:

- [ ] Pass `agent_session_id` in the **request body**, not the `x-agent-session-id` header. The
      header is ignored unless `previous_response_id` is also present.
- [ ] Send `agent_session_id` **and** `previous_response_id` together from turn two onward.
- [ ] Read the returned id from the body *and* the header; tolerate either being absent.
- [ ] Do not key state by anything from `context`. Use a constant.
- [ ] Remember `$HOME` persists across idle periods, so treat it as durable, not as a cache.
- [ ] Pre-create sessions with `POST {endpoint}/sessions` when the UI opens. See
      [04-cold-start-and-scaling.md](04-cold-start-and-scaling.md).

If you build the hybrid:

- [ ] Keep the router stateless. The moment it remembers a session, it needs a database and you
      are back to self-hosting economics.
- [ ] Return both handles to the caller and require them on the next turn.
- [ ] Expose a `/prewarm` endpoint so the client can provision a sandbox before the user types.
