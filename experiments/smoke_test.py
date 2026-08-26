"""Local smoke test: runs the shared agent with no hosting in the way.

Before comparing two hosting models it is worth proving the agent itself works,
otherwise the first failed deployment sends you debugging the wrong layer. This
script runs the exact same `agent_core` that both deployments run, against the
real Foundry project, with state on local disk.

    python experiments/smoke_test.py

Expected: the third turn reports Paris and Rome, in that order.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_core import DiskStore, build_agent, build_chat_client  # noqa: E402

TURNS = ["Add Paris to my trip", "Add Rome", "What is in my trip?"]


async def main() -> int:
    os.environ.setdefault("STATE_DIR", str(Path(__file__).parent / "_smoke_state"))
    store = DiskStore()
    session_id = f"smoke-{uuid.uuid4().hex[:8]}"
    client = build_chat_client()

    print(f"session: {session_id}")
    print(f"project: {os.environ.get('AZURE_AI_PROJECT_ENDPOINT')}\n")

    for turn in TURNS:
        # A fresh Agent per turn on purpose: it proves the itinerary survives in
        # the store rather than in the agent object's memory.
        agent = build_agent(store, session_id, client=client)
        response = await agent.run(turn)
        print(f">> {turn}")
        print(f"<< {response.text.strip()}\n")

    final = await store.load(session_id)
    destinations = [d.lower() for d in final.destinations]
    print(f"stored state: {final}")

    ok = destinations == ["paris", "rome"]
    print("\nRESULT:", "PASS" if ok else "FAIL - expected ['paris', 'rome']")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
