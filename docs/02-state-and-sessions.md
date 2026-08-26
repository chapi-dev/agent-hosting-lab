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
and its own `$HOME`, and nothing reaches it without presenting that session's id. There is
nothing left to key by. A constant path inside a private sandbox is isolated by construction.

Note the precise wording — *without presenting that session's id*. That qualifier is doing more
work than it looks, and Finding 4 is about what happens when someone else presents it.

Compare the blast radius of getting the key wrong in each model:

- **Self-hosted:** the session id is load-bearing. Get it wrong and one user can read another
  user's data. It is a security incident.
- **Hosted:** get it wrong and one conversation loses its own state. It is a bug.

The isolation is structural rather than something the code must get right. That difference —
not the line count — is the strongest argument for hosted runtimes in a multi-tenant,
employee-facing deployment.

---

## Finding 4: the session id *is* the isolation boundary, and that changes what the router owes you

Finding 3 leans on sandbox isolation. It is worth knowing exactly what that isolation is made
of, because the answer decides how much security work the hybrid pattern still owes you.

We measured it (`experiments/06_session_isolation.py`, part 1). Three facts, in order:

**1. The runtime accepts session ids it never issued.** Send `agent_session_id` as 64
characters of `a` and it works. The id is a name the caller chooses, not a token the platform
mints.

**2. Knowing the id is enough to read the session's state — no conversation history required.**

```
turn A   agent_session_id=X, "Add Barcelona to my trip"   -> "Added Barcelona to your trip."
turn B   agent_session_id=X, NEW conversation,
         no previous_response_id, "What is in my trip?"   -> "Your trip includes: Barcelona."
control  agent_session_id=Y, same question                -> "Your trip is empty."
```

Turn B shares nothing with turn A except the session id. The control rules out the model simply
being agreeable.

**3. The runtime always echoes back the id you sent**, valid or not. So you cannot detect a
failed reattach by inspecting the response. Latency is the only honest signal — which is why
[04-cold-start-and-scaling.md](04-cold-start-and-scaling.md) measures it instead of trusting a
field.

### Why this is not a runtime bug

Every call above carried the same Azure AD identity. This is not a bypass between principals,
and the runtime is behaving correctly: when each end user calls with their own credentials,
Entra has already separated them and the session id only has to select *which* of that user's
sandboxes to open.

The hybrid pattern breaks that assumption. The router calls the runtime with **one managed
identity on behalf of every user**, so from the runtime's side all your users are the same
principal. Whatever the router accepts from one client, it will happily perform for any client.

> **The consequence:** the moment you put a router in front of a hosted agent, session
> authorization becomes *yours*. The platform cannot do it for you, because you have taken away
> the only thing it could have used to tell your users apart.

### What the router does instead

Never accept a raw session id from a client. Issue a **handle** that binds the session to the
user it was created for:

```python
handle = f"{agent_session_id}.{b64url(HMAC_SHA256(SESSION_SECRET, f'{agent_session_id}|{user}'))}"
```

- `/prewarm` returns the handle and **never** the raw id.
- `/chat` recomputes the signature for *the calling user* and rejects a mismatch with 403. A
  handle minted for Alice is useless to Mallory, and a raw id is useless to everybody.
- Verification is a hash, so the router stays **stateless** — no session table, no database, and
  it still scales to any number of replicas.
- Use `hmac.compare_digest`, not `==`. This is a signature check on attacker-controlled input,
  which is exactly where timing side channels live.
- The signing key must be **shared across replicas**. Generate it per process and requests will
  fail 403 at random, in proportion to your replica count — a spectacularly confusing bug.

Part 2 of the experiment verifies all of it against the deployed router:

| Attempt | Result |
|---|---|
| Owner replays their own handle | **200**, sees their own state |
| Another user replays that handle verbatim | **403** |
| Another user sends a raw platform-shaped id | **403** |

### The honest scorecard

This finding does *not* undo Finding 3 — it prices it. Sandbox isolation is real and you still
get it for free. What you do not get for free is *authorization*, and only in the hybrid shape:

| Shape | Who separates users? |
|---|---|
| Client → hosted agent, user's own credentials | Entra. Nothing to build. |
| Client → router → hosted agent (hybrid) | **You.** 34 measured lines of HMAC, and you must not skip them. |
| Self-hosted | You, for both isolation *and* state. |

Thirty-four lines is a small price — 6% of what self-hosting costs, and it does not grow as you
add agents. But it belongs on the estimate, and it belongs in the security review: silently
inheriting "the platform isolates sessions" from a diagram where the arrows have changed is how
this gets missed.

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
- [ ] **Never accept a raw `agent_session_id` from a client.** Issue a signed handle bound to the
      caller and reject handles that do not match. See Finding 4 — the router is now the only
      thing separating your users.
- [ ] Share the signing key across replicas, and derive the user identity from a validated token,
      not a header.
- [ ] Return both handles to the caller and require them on the next turn.
- [ ] Expose a `/prewarm` endpoint so the client can provision a sandbox before the user types.
