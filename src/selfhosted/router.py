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

import logging
import os
import socket
import time
from contextlib import asynccontextmanager

import httpx
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("router")

REPLICA = os.environ.get("CONTAINER_APP_REPLICA_NAME") or socket.gethostname()
SCOPE = "https://ai.azure.com/.default"

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    endpoint = os.environ.get("HOSTED_AGENT_ENDPOINT", "").rstrip("/")
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
    # Returned by the router on every reply and echoed back by the caller.
    #
    # The router deliberately does not remember these. Keeping a session map
    # here would give the router state, and state is exactly what we pushed down
    # into the hosted runtime so the router could scale freely. Passing the
    # handles through the client keeps every replica interchangeable.
    #
    # Two handles, and they do different jobs:
    #   agent_session_id      selects the sandbox (the agent's $HOME)
    #   previous_response_id  continues the conversation (message history)
    #
    # Send both once you have both. On turn one only agent_session_id exists,
    # and it is enough - see the comment in chat() for why it must travel in the
    # request body rather than the x-agent-session-id header.
    agent_session_id: str | None = None
    previous_response_id: str | None = None


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "replica": REPLICA,
        "variant": os.environ.get("VARIANT", "hybrid-router"),
        "state_backend": "none (delegated to hosted agent)",
        "hosted_endpoint_configured": bool(_state.get("endpoint")),
        "uptime_s": round(time.time() - _state["started"], 1),
    }


@app.post("/prewarm")
async def prewarm() -> dict:
    """Provisions a sandbox before the user has typed anything.

    Call this when the chat window opens, not when the user sends a message.
    Sandbox provisioning costs 6-7 seconds and the user is going to spend at
    least that long reading the greeting, so the wait costs nothing if it starts
    early. Measured effect: the first real turn drops from ~9.5s to ~3.2s.

    Fire and forget from the UI's point of view - do not block rendering on it.
    Hand the returned agent_session_id to /chat with the user's first message.

    If this call fails, the client should simply omit agent_session_id. The
    runtime allocates a sandbox on demand, so the conversation still works; it
    is just slower on turn one. Pre-warming is an optimisation, never a
    dependency.
    """
    endpoint = _state.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=503, detail="HOSTED_AGENT_ENDPOINT not configured")

    started = time.perf_counter()
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
    return {
        "agent_session_id": body.get("agent_session_id"),
        "expires_at": body.get("expires_at"),
        "replica": REPLICA,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


@app.post("/chat")
async def chat(req: ChatRequest) -> dict:
    endpoint = _state.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=503, detail="HOSTED_AGENT_ENDPOINT not configured")

    started = time.perf_counter()
    intent = classify(req.message)
    agent = ROUTES[intent]

    if not authorize(agent, set(req.groups) or {"*"}):
        raise HTTPException(status_code=403, detail=f"not entitled to {agent}")

    token = _state["credential"].get_token(SCOPE).token
    payload = {"input": req.message, "store": True}
    if req.previous_response_id:
        payload["previous_response_id"] = req.previous_response_id
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if req.agent_session_id:
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
        payload["agent_session_id"] = req.agent_session_id

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
    return {
        "reply": _extract_text(body),
        "replica": REPLICA,
        "variant": "hybrid-router",
        "routed_to": agent,
        "intent": intent,
        "state_backend": "hosted-session",
        "agent_session_id": (
            resp.headers.get("x-agent-session-id")
            or body.get("agent_session_id")
            or req.agent_session_id
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
