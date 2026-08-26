"""Self-hosted deployment: the agent wrapped in a web server you own.

Everything in this file exists because nobody else is going to provide it. The
HTTP surface, the session id extraction, the health probe, the telemetry wiring,
the graceful shutdown - none of it is agent logic, and all of it is required
before the agent can serve a single request. That is the honest cost of
self-hosting, and it is the reason this file is longer than its hosted
counterpart in `src/hosted/main.py`, which does the same job in a fraction of
the lines.

The `/chat` response deliberately reports which replica answered. When the naive
variant loses state you can see in the response body that the second turn landed
somewhere else, which turns an abstract warning about stateless containers into
something you can point at.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core import build_agent, build_chat_client, store_from_env  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("selfhosted")

REPLICA = os.environ.get("CONTAINER_APP_REPLICA_NAME") or socket.gethostname()
VARIANT = os.environ.get("VARIANT", "selfhosted")

_state: dict = {}


def _configure_telemetry() -> bool:
    """Wires OpenTelemetry to Application Insights.

    The hosted runtime does this for you: it injects the connection string and
    the protocol libraries emit spans without being asked. Here it is opt-in
    code that somebody has to remember to write, keep working, and carry into
    every new service. Note that it is also allowed to fail silently, because a
    telemetry outage must not take the agent down - another decision the hosted
    model does not ask you to make.
    """
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        logger.warning("no APPLICATIONINSIGHTS_CONNECTION_STRING; telemetry disabled")
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=conn, logger_name="selfhosted")
        logger.info("telemetry configured")
        return True
    except Exception as exc:  # pragma: no cover - telemetry must never be fatal
        logger.warning("telemetry setup failed: %s", exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["telemetry"] = _configure_telemetry()
    _state["store"] = store_from_env()
    _state["client"] = build_chat_client()
    _state["started"] = time.time()
    logger.info("replica %s ready, state backend=%s", REPLICA, _state["store"].backend)
    yield
    store = _state.get("store")
    if store is not None and hasattr(store, "close"):
        await store.close()


app = FastAPI(title=f"trip-planner ({VARIANT})", lifespan=lifespan)


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    reply: str
    replica: str
    variant: str
    state_backend: str
    itinerary: list[str]
    latency_ms: int


@app.get("/health")
async def health() -> dict:
    """Container Apps polls this. If it lies, traffic goes to a broken replica."""
    return {
        "status": "ok",
        "replica": REPLICA,
        "variant": VARIANT,
        "state_backend": _state["store"].backend,
        "uptime_s": round(time.time() - _state["started"], 1),
        "telemetry": _state["telemetry"],
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    started = time.perf_counter()
    store = _state["store"]
    try:
        agent = build_agent(store, req.session_id, client=_state["client"])
        response = await agent.run(req.message)
    except Exception as exc:
        logger.exception("chat failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    itinerary = await store.load(req.session_id)
    return ChatResponse(
        reply=response.text.strip(),
        replica=REPLICA,
        variant=VARIANT,
        state_backend=store.backend,
        itinerary=list(itinerary.destinations),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
