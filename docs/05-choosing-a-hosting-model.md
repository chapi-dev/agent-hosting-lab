# 5. Choosing a hosting model

A decision procedure, not a feature table. Feature tables compare documentation; this compares
what the lab actually observed.

## Start here: the two questions that decide it

Almost every real case is settled by two questions. Answer them before reading anything else.

**Q1. Does the agent runtime itself need to reach a network the platform cannot reach?**

Not your orchestration layer — the *agent runtime*, the thing calling tools. On-premises APIs,
private endpoints, IP-allow-listed third parties.

- **Yes** → the runtime must be self-hosted, or the tool call must be proxied through something
  that is. Prefer the proxy: it keeps the runtime hosted and the private egress in a thin layer
  you own. That is the hybrid.
- **No** → continue.

**Q2. Do you need a first answer in under ~2 seconds on a brand-new session, with no
opportunity to pre-warm?**

- **Yes** → self-host. Hosted sandbox provisioning costs ~8–10 s and pre-warming requires a
  moment before the user's first message. If there is genuinely no such moment, this is a real
  blocker. See [04-cold-start-and-scaling.md](04-cold-start-and-scaling.md).
- **No** (there is a chat window opening, a page load, a greeting — anything) → **hosted or
  hybrid.**

Most enterprise assistants answer *no* to both. That is the finding.

## The third question: do you need routing or authorization?

This one does not decide *hosted vs self-hosted*. It decides *hosted vs hybrid*.

- Multiple agents behind one entry point, chosen by intent?
- Authorization decisions — which user may reach which agent — evaluated **before** dispatch?
- Guardrails that must run before a request reaches any model?
- Business logic that must run regardless of what the model decides?

If any of these are yes, you need a component you own in front of the runtime. That component is
small, stateless, and does not need to run the agent. **That is the hybrid, and it is the answer
for most enterprise platforms.**

## Requirement-by-requirement

A generic enterprise agent platform's requirements, assessed against what this lab measured.

| Requirement | Self-hosted | Hosted | Hybrid | Notes |
|---|---|---|---|---|
| Central orchestration / intent routing | ✅ | ⚠️ | ✅ | Hosted can route agent-to-agent, but authorization-aware routing wants your code |
| Backend integration via a gateway (MCP, APIM) | ✅ | ✅ | ✅ | Both call outbound HTTP |
| Header forwarding for per-session backend auth | ✅ | ⚠️ | ✅ | Router is the natural place to inject and scrub headers |
| Structured tool results (JSON to the UI) | ✅ | ✅ | ✅ | Agent-level concern, not hosting |
| Attachments / document validation | ✅ | ✅ | ✅ | Hosted sandbox gives durable `$HOME` for scratch files |
| Proactive / async user notifications | ✅ | ⚠️ | ✅ | Needs a component that can initiate; that is the router or a worker |
| **Robust concurrent session handling** | ❌ you build it | ✅ structural | ✅ structural | The measured failure. See [02](02-state-and-sessions.md) |
| Multi-turn via Responses protocol | ✅ | ✅ | ✅ | Hosted implements it natively |
| Evaluation of agents and workflows | ✅ | ✅ | ✅ | Foundry evaluation works against both |
| **Scale to thousands of users** | ⚠️ shared-store bottleneck | ✅ per-session isolation | ✅ | See [04](04-cold-start-and-scaling.md) |
| Code-first CI/CD | ✅ | ✅ | ✅ | `azd` for hosted; container pipeline for self |
| Custom guardrails before dispatch | ✅ | ⚠️ | ✅ | Pre-dispatch guardrails need your code in the path |
| Arbitrary Python business logic | ✅ | ✅ | ✅ | Both run your code |
| **Authorization-aware routing** | ✅ | ❌ | ✅ | The decisive requirement for the hybrid |
| Extensibility (add agents over time) | ✅ | ✅ | ✅ | Hybrid: a dict entry plus a deployment |
| Private network egress | ✅ | ❌ | ✅ | Put it in the router |

Read the bold rows. Neither pure model satisfies all of them. The hybrid does.

## Why not pure hosted

Two gaps, both real:

1. **Authorization-aware routing.** Deciding *which* agent a user may reach, based on their
   groups, roles or attributes, before any model sees the request. The hosted runtime executes
   an agent; it does not decide entitlement to one, and neither does Azure RBAC in this shape —
   the router calls the runtime with its own managed identity, so the platform sees one principal
   regardless of which end user started the request. `ENTITLEMENTS` in `src/selfhosted/router.py`
   is fifteen lines and belongs in code you own and audit. Measured 4/4 against the deployment
   (`experiments/07_authorization_routing.py`), including the case that matters most: a caller
   presenting no groups is refused a restricted agent.
2. **Private network egress.** If a tool must call an on-premises system, something inside your
   network has to make that call.

Both are satisfied by a small stateless service. Neither requires self-hosting the *runtime*.

## Why not pure self-hosted

Four costs, all measured:

1. **9.94x the supporting code** — 527 lines versus 53. See [03](03-the-cost-of-self-hosting.md).
2. **Session isolation becomes a security boundary you own.** Get the key wrong self-hosted and
   one user reads another's data. Get it wrong hosted and one conversation loses its own state.
3. **Governance surface.** This lab needed a VNet, a private endpoint, a private DNS zone and an
   environment rebuild solely because org policy disabled public access on the state store it
   only needed because it was self-hosting. The hosted agent needed none of it.
4. **A shared state store is a scaling bottleneck and a correlated failure.** Every conversation
   depends on it simultaneously.

## The recommendation

**Hybrid: own the routing, rent the runtime.**

```
client ──▶ router (self-hosted, stateless) ──▶ hosted agent runtime
             • intent classification              • per-session sandbox
             • authorization                      • state, scaling, identity
             • signed session handles             • telemetry
             • private egress
             • guardrails
```

Cost: 175 lines and ~200 ms of proxy latency.

Buys: session-state correctness for free, no state backend, no VNet requirement for the runtime,
no Dockerfile for the agent, plus full control over routing and authorization.

The router **must stay stateless**. The moment it remembers a session it needs a database, and
you have reintroduced self-hosting economics through the side door. Return both session handles
to the caller and require them on the next turn — `src/selfhosted/router.py` shows the pattern.

**And it must sign those handles.** This is the hybrid's one hidden obligation: the router calls
the runtime with a single managed identity for every user, so the runtime can no longer tell
your users apart, and session authorization silently becomes yours. 34 of the 175 lines are
exactly that. See [02, Finding 4](02-state-and-sessions.md#finding-4-the-session-id-is-the-isolation-boundary-and-that-changes-what-the-router-owes-you)
— we shipped the naive version first, so this is a warning rather than a theory.

## Migration path

You do not have to choose once and forever, and the ordering below reduces risk at each step.

1. **Build the agent against `agent_core`-style shared logic.** Keep hosting concerns out of the
   agent. This lab's four variants share 122 lines; that is what makes moving cheap.
2. **Start hosted.** Fastest path to something working: 53 lines and `azd ai agent deploy`.
   See [runbooks/01](../runbooks/01-deploy-hosted-agent.md).
3. **Add the router when you need the second agent** — or sooner if you need authorization from
   day one. See [runbooks/03](../runbooks/03-deploy-hybrid-router.md).
4. **Self-host only the specific agent that needs it**, if one ever does. The hybrid already has
   the routing layer, so adding a self-hosted target is a dictionary entry, not a rewrite.

Step 4 is why the hybrid is future-proof rather than merely adequate: it is the only shape that
lets you change hosting model per-agent without changing the architecture.

## Anti-patterns

Things this lab did wrong, or nearly did, so you do not have to.

| Anti-pattern | Why it fails |
|---|---|
| Session state on local disk, multiple replicas | Silent data loss. The measured failure in [02](02-state-and-sessions.md) |
| Testing an agent with one replica | The failure mode requires two. Staging will pass and production will not |
| Sending `x-agent-session-id` without `previous_response_id` | The header is ignored; a new sandbox is allocated every turn |
| Keying hosted state by `context.conversation_chain_id` | Not stable across turns. Use a constant |
| A stateful router | Reintroduces the state backend you just eliminated |
| Choosing self-hosting on compute price | Compute is the smallest cost. Code, governance and incidents dominate |
| Measuring cold start without pre-warming | Overstates the hosted penalty by ~70% |
| Declaring your own copy of the project endpoint | The platform injects `FOUNDRY_PROJECT_ENDPOINT`. A copy can shadow it and drift |
