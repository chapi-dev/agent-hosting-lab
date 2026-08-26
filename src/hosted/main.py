"""Foundry hosted deployment: the same agent, with the platform underneath it.

Read this file next to `src/selfhosted/server.py`. They run identical agent
logic. This one is roughly a third of the length, and the difference is not
cleverness - it is everything that is simply absent:

  no web framework          the host provides the Responses HTTP surface
  no session plumbing       the platform gives each session its own sandbox
  no Dockerfile             the platform builds and ships the image
  no telemetry setup        APPLICATIONINSIGHTS_CONNECTION_STRING is injected
                            and the protocol library emits spans by default
  no health probe           the platform owns liveness
  no state backend          $HOME survives across turns and idle periods

That last one is worth dwelling on. This file imports `DiskStore` - the same
class that is a bug in the naive self-hosted variant. Here it is correct,
because the sandbox is per-session and durable. The agent gets isolated,
persistent state without the code ever learning what a session id is.
"""

from __future__ import annotations

import inspect
import os

from azure.ai.agentserver.responses import ResponsesAgentServerHost, TextResponse

from agent_core import DiskStore, build_agent, build_chat_client

# $HOME is per-session and persisted by the platform between turns and across
# idle periods. On a self-hosted container the same path is ephemeral and
# per-replica. Identical code, opposite guarantees.
STORE = DiskStore(os.environ.get("STATE_DIR") or os.path.expanduser("~/trip"))

app = ResponsesAgentServerHost()
_client = build_chat_client()


@app.response_handler
async def handle(request, context, cancellation_signal):
    """Handles one turn.

    The signature is fixed by the runtime and must be exactly these three
    positional parameters, in this order. A two-argument handler raises
    TypeError at import time, the sandbox never reaches readiness, and the
    caller sees HTTP 424 session_not_ready - an error that says nothing about
    function signatures. Worth knowing before you spend an evening on it.

    The return value must be a response object, not a string. Returning a bare
    string fails with `'async for' requires an object with __aiter__ method,
    got str`, because the runtime streams whatever the handler returns.

    Note the state key: a constant. This looks wrong and is the most important
    line in the file.

    The sandbox is already per-session - the platform gives this conversation
    its own VM and its own $HOME, and no other conversation can see either. So
    there is nothing to key by. A constant path inside a private sandbox is
    isolated by construction.

    We first tried keying by `context.conversation_chain_id`, which reads more
    defensively and is wrong: the value is not stable across turns, so each turn
    wrote a different file and the itinerary vanished. Turn 3 answered "Your
    trip is empty" after two successful adds - no error, no warning, just a
    confident false statement.

    Compare with `src/selfhosted/server.py`, where the session id is load-bearing
    and getting it wrong means one user reading another user's data. Here the
    blast radius of the same mistake is one conversation losing its own state,
    because the isolation is structural rather than something the code has to
    get right.
    """
    agent = build_agent(STORE, "session", client=_client)
    result = await agent.run(await _input_text(context))
    return TextResponse(context, request, text=result.text.strip())


async def _input_text(context) -> str:
    """Reads the user's text, tolerating sync and async runtimes.

    `get_input_text()` is a coroutine in the deployed runtime and a plain method
    in some local builds. Passing the un-awaited coroutine straight to the agent
    fails deep inside telemetry with `TypeError: 'coroutine' object is not
    iterable`, which points at agent_framework internals rather than at this
    line. Awaiting conditionally keeps `azd ai agent run` and the deployed agent
    on the same code path.
    """
    value = context.get_input_text()
    if inspect.isawaitable(value):
        value = await value
    return value or ""


if __name__ == "__main__":
    app.run()
