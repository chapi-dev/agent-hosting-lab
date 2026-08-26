"""The hybrid router: self-hosted orchestration in front of a hosted runtime.

This is the pattern the lab exists to argue for, so it is worth being precise
about why it is shaped this way.

The router is self-hosted because it does things the platform does not model:
it decides which agent should handle a request, it applies authorization before
routing, and it can reach private networks. Those are the genuine reasons to run
your own compute, and they have nothing to do with running the model.

The agent runtime is hosted because session state, sandboxing, scaling, identity
and telemetry are solved problems that the platform solves better than a team
will solve them again. The router holds no session state at all - which is why
it scales to five replicas without a Cosmos account, a partition key, or a
single line of the state-management code in `agent_core/state.py`.

Own the routing. Rent the runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import socket
import time
from contextlib import asynccontextmanager

import httpx
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("router")

REPLICA = os.environ.get("CONTAINER_APP_REPLICA_NAME") or socket.gethostname()
SCOPE = "https://ai.azure.com/.default"

# Signs the session handles the router hands out. Must be identical on every
# replica - a per-process random value would make handles issued by one replica
# unverifiable on another. In production this comes from Key Vault.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").encode()

_state: dict = {}

# Intent -> hosted agent. One entry today; the point is that adding the second
# is a dictionary entry and a deployment, not an architecture change.
ROUTES = {
    "trip": "trip-planner",
}

# Which groups may reach which agent. Authorization lives here, in code you own,
# evaluated before the request ever reaches a runtime. This is exactly the
# "authorization-aware routing" requirement that pure hosted-only designs
# struggle with, and exactly why the hybrid shape wins.
ENTITLEMENTS = {
    "trip-planner": {"*"},
}


def classify(message: str) -> str:
    """Toy intent classifier.

    A real deployment would call a small model or a classifier service. Keeping
    it trivial here is deliberate: the lab is measuring hosting, not routing
    quality, and a deterministic classifier keeps the experiments repeatable.
    """
    lowered = message.lower()
    if any(word in lowered for word in ("trip", "travel", "itinerary", "city", "visit")):
        return "trip"
    return "trip"


def authorize(agent: str, groups: set[str]) -> bool:
    allowed = ENTITLEMENTS.get(agent, set())
    return "*" in allowed or bool(allowed & groups)


# --------------------------------------------------------- session handles ---
#
# Measured against the deployed runtime, and the reason this code exists:
#
#   1. The runtime accepts ANY agent_session_id the caller sends, including one
#      the platform never issued. We sent 64 'a' characters and got a working
#      sandbox back.
#   2. Sending a session id is sufficient to read that sandbox's state. A brand
#      new conversation, with no previous_response_id, asked "what is in my
#      trip?" against another conversation's id and was told "Barcelona" - the
#      value the other conversation had stored. A control with a different id
#      got "your trip is empty".
#   3. The runtime echoes the id back on every response, whatever you send, so
#      you cannot tell a genuine reattach from a fabricated one by inspecting
#      the reply.
#
# Together those mean the session id IS the isolation boundary. That is fine
# when each end user calls the runtime with their own credentials. It is not
# fine in the hybrid pattern, where the router calls the runtime with its own
# managed identity on behalf of everyone: there the runtime cannot tell users
# apart, so anything the router accepts from a client, it accepts from every
# client.
#
# So the router never accepts a raw session id. It issues a handle that binds
# the session to the user it was created for, and refuses handles that do not
# match the caller. Verification is a hash - it needs no database, so the router
# stays stateless and scales freely.


def issue_handle(agent_session_id: str, user: str) -> str:
    """Binds a platform session id to a user, in a string safe to give a client."""
    signature = hmac.new(
        SESSION_SECRET, f"{agent_session_id}|{user}".encode(), hashlib.sha256
    ).digest()
    return f"{agent_session_id}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def open_handle(handle: str, user: str) -> str:
    """Returns the session id inside `handle`, or raises 403.

    compare_digest rather than `==`: handle verification is a signature check on
    attacker-supplied input, and that is where timing side channels live.
    """
    agent_session_id, _, _ = handle.rpartition(".")
    if not agent_session_id or not hmac.compare_digest(handle, issue_handle(agent_session_id, user)):
        raise HTTPException(status_code=403, detail="session handle is not valid for this user")
    return agent_session_id


def identify(x_user_id: str | None) -> str:
    """The end user this request is for.

    The lab reads a header so the mechanism is easy to exercise. **In production
    this must come from a validated token** - the object id of the caller in the
    incoming JWT, checked by Easy Auth, API Management or the router itself.
    Trusting a plain header in production would let a client name any user and
    receive handles bound to them.

    The same applies to `groups` on ChatRequest: entitlements are only as
    trustworthy as the claim they are derived from.
    """
    return (x_user_id or "anonymous").strip() or "anonymous"


def normalize_endpoint(raw: str) -> str:
    """Reduces whatever form of the agent endpoint you have to the protocol base.

    There are two URLs in circulation for the same agent and it is easy to be
    handed the wrong one:

        .../endpoint/protocols/openai                          <- protocol base
        .../endpoint/protocols/openai/responses?api-version=v1 <- responses URL

    `azd env get-value AGENT_<NAME>_RESPONSES_ENDPOINT` returns the second, while
    the router needs the first so it can append its own path. Concatenating them
    blindly yields `.../responses?api-version=v1/responses?api-version=v1`, which
    fails with a misleading `API version not supported`. Rather than making the
    caller remember which is which, accept both.
    """
    base = raw.strip().split("?")[0].rstrip("/")
    if base.endswith("/responses"):
        base = base[: -len("/responses")]
    return base


@asynccontextmanager
async def lifespan(app: FastAPI):
    endpoint = normalize_endpoint(os.environ.get("HOSTED_AGENT_ENDPOINT", ""))
    if not endpoint:
        logger.warning("HOSTED_AGENT_ENDPOINT not set; router will return 503")
    _state["endpoint"] = endpoint
    _state["credential"] = DefaultAzureCredential()
    _state["http"] = httpx.AsyncClient(timeout=120.0)
    _state["started"] = time.time()
    yield
    await _state["http"].aclose()


app = FastAPI(title="trip-planner (hybrid router)", lifespan=lifespan)


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1)
    groups: list[str] = Field(default_factory=list)
    # Returned by /prewarm and by every /chat reply, echoed back by the caller.
    #
    # This is a *handle*, not the platform's session id: the raw id plus a
    # signature binding it to the user it was issued for. The router refuses to
    # forward a handle whose signature does not match the caller, which is what
    # stops one user reading another user's sandbox by learning its id. See the
    # session handles section above for the measurements behind that.
    #
    # The router deliberately remembers nothing. Keeping a session map here
    # would give the router state, and state is exactly what we pushed down into
    # the hosted runtime so the router could scale freely. Passing a signed
    # handle through the client keeps every replica interchangeable.
    session_handle: str | None = None
    # Continues the conversation (message history). The handle selects the
    # sandbox; this selects the transcript. Two handles, two different jobs.
    previous_response_id: str | None = None


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "replica": REPLICA,
        "variant": os.environ.get("VARIANT", "hybrid-router"),
        "state_backend": "none (delegated to hosted agent)",
        "hosted_endpoint_configured": bool(_state.get("endpoint")),
        # Exposed so a misconfigured endpoint is visible without reading logs:
        # this is the normalized protocol base the router will actually call.
        "hosted_endpoint": _state.get("endpoint") or None,
        # Without a shared secret the router cannot issue or verify handles, and
        # every session call fails closed. Surfaced here because the symptom
        # otherwise looks like a session bug rather than a missing setting.
        "session_signing_configured": bool(SESSION_SECRET),
        "uptime_s": round(time.time() - _state["started"], 1),
    }


@app.post("/prewarm")
async def prewarm(x_user_id: str | None = Header(default=None)) -> dict:
    """Provisions a sandbox before the user has typed anything.

    Call this when the chat window opens, not when the user sends a message.
    Sandbox provisioning costs 6-7 seconds and the user is going to spend at
    least that long reading the greeting, so the wait costs nothing if it starts
    early. Measured effect: the first real turn drops from ~9.5s to ~3.2s.

    Fire and forget from the UI's point of view - do not block rendering on it.
    Hand the returned session_handle to /chat with the user's first message.

    If this call fails, the client should simply omit session_handle. The
    runtime allocates a sandbox on demand, so the conversation still works; it
    is just slower on turn one. Pre-warming is an optimisation, never a
    dependency.
    """
    endpoint = _state.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=503, detail="HOSTED_AGENT_ENDPOINT not configured")

    started = time.perf_counter()
    user = identify(x_user_id)
    if not SESSION_SECRET:
        raise HTTPException(status_code=503, detail="SESSION_SECRET not configured")

    token = _state["credential"].get_token(SCOPE).token
    # Sessions hang off the agent endpoint root, not off the protocol path.
    # .../endpoint/sessions works; .../endpoint/protocols/openai/sessions is 404.
    url = endpoint.split("/protocols/")[0] + "/sessions?api-version=v1"
    try:
        resp = await _state["http"].post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("session pre-creation failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    body = resp.json()
    agent_session_id = body.get("agent_session_id")
    return {
        # Bound to this user. The raw platform id is deliberately not returned:
        # it is the only thing protecting the sandbox, so it never leaves here.
        "session_handle": issue_handle(agent_session_id, user),
        "expires_at": body.get("expires_at"),
        "replica": REPLICA,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


@app.post("/chat")
async def chat(req: ChatRequest, x_user_id: str | None = Header(default=None)) -> dict:
    endpoint = _state.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=503, detail="HOSTED_AGENT_ENDPOINT not configured")

    started = time.perf_counter()
    user = identify(x_user_id)
    intent = classify(req.message)
    agent = ROUTES[intent]

    if not authorize(agent, set(req.groups) or {"*"}):
        raise HTTPException(status_code=403, detail=f"not entitled to {agent}")

    agent_session_id = None
    if req.session_handle:
        if not SESSION_SECRET:
            raise HTTPException(status_code=503, detail="SESSION_SECRET not configured")
        # Raises 403 if this handle was issued to somebody else, or forged.
        agent_session_id = open_handle(req.session_handle, user)

    token = _state["credential"].get_token(SCOPE).token
    payload = {"input": req.message, "store": True}
    if req.previous_response_id:
        payload["previous_response_id"] = req.previous_response_id
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if agent_session_id:
        # Selects the caller's sandbox on the hosted runtime.
        #
        # This goes in the *body*, not in the x-agent-session-id header, and the
        # difference is measurable rather than stylistic. Experiment 5 ran both
        # against pre-created sessions: the body field reattached in 4/4 rounds
        # and the first turn took 3.2s; the header reattached in 0/4 and the
        # first turn took 9.5s, because the runtime quietly allocated a fresh
        # sandbox instead.
        #
        # The header is only honoured alongside previous_response_id, which by
        # definition does not exist on turn one - exactly the turn where a
        # pre-warmed session is worth anything. The body field has no such
        # dependency, so it is the mechanism to use.
        payload["agent_session_id"] = agent_session_id

    try:
        resp = await _state["http"].post(
            f"{endpoint}/responses?api-version=v1", headers=headers, json=payload
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("hosted agent returned %s: %s", exc.response.status_code, exc.response.text[:500])
        raise HTTPException(status_code=502, detail=exc.response.text[:500]) from exc
    except httpx.HTTPError as exc:
        logger.exception("hosted agent call failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    body = resp.json()
    # The runtime reports a session id on every response - but it reports back
    # whatever you sent it, including an id it never issued. We checked: 64 'a'
    # characters came back verbatim. So this value cannot be used to tell a
    # genuine reattach from a fabricated one, and the router does not pretend
    # otherwise. Latency is the only honest signal that a session was reused.
    runtime_session_id = resp.headers.get("x-agent-session-id") or body.get("agent_session_id")
    return {
        "reply": _extract_text(body),
        "replica": REPLICA,
        "variant": "hybrid-router",
        "routed_to": agent,
        "intent": intent,
        "state_backend": "hosted-session",
        # Re-issued rather than echoed: on turn one the runtime allocates the
        # session, and the caller needs a handle bound to them to continue.
        "session_handle": (
            issue_handle(runtime_session_id, user)
            if runtime_session_id and SESSION_SECRET
            else req.session_handle
        ),
        "previous_response_id": body.get("id"),
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def _extract_text(body: dict) -> str:
    """Pulls the assistant text out of a Responses payload.

    The shape varies a little between protocol versions, so this tolerates both
    the flattened `output_text` and the nested `output[].content[].text` form
    rather than assuming one and failing loudly in the middle of an experiment.
    """
    if isinstance(body.get("output_text"), str) and body["output_text"]:
        return body["output_text"].strip()
    chunks: list[str] = []
    for item in body.get("output", []) or []:
        for part in item.get("content", []) or []:
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
            elif isinstance(text, dict) and isinstance(text.get("value"), str):
                chunks.append(text["value"])
    return "\n".join(chunks).strip() or "(no text in response)"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
