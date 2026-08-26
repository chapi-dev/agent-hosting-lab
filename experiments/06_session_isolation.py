"""Experiment 6: what actually isolates one user's session from another's.

Hosted agents give every session its own sandbox, and that sandbox is where the
agent's state lives. The obvious question - and the one nobody asks until an
audit - is what stops user B from reading user A's sandbox.

This experiment answers it by trying, twice:

  Part 1, against the hosted runtime directly
      Write a value into a session, then open a *brand new conversation* that
      supplies only that session id - no previous_response_id, no shared
      history - and ask for the value back. A control does the same with a
      different id.

  Part 2, against the hybrid router
      Repeat the attack through the router, where user B presents a handle
      issued to user A.

Part 1 is not a vulnerability in the runtime. When each end user calls with
their own credentials, the runtime is entitled to treat the session id as
sufficient. It becomes a problem in the hybrid pattern, where the router calls
the runtime with a single managed identity for everybody: at that point the
runtime cannot tell users apart, so whatever the router accepts from one client
it accepts from all of them. Part 2 checks that the router closes that gap.

    python experiments/06_session_isolation.py

Exit code is non-zero if the router fails to contain the attack.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

RESULTS = Path(__file__).parent / "results"
SCOPE = "https://ai.azure.com/.default"
SECRET_VALUE = "Barcelona"


def text_of(body: dict) -> str:
    if isinstance(body.get("output_text"), str) and body["output_text"]:
        return body["output_text"]
    chunks = []
    for item in body.get("output", []) or []:
        for part in item.get("content", []) or []:
            value = part.get("text")
            if isinstance(value, str):
                chunks.append(value)
            elif isinstance(value, dict) and isinstance(value.get("value"), str):
                chunks.append(value["value"])
    return "\n".join(chunks)


def probe_runtime(endpoint: str) -> dict:
    """Can a session's state be read knowing only its id?"""
    credential = DefaultAzureCredential()
    auth = {
        "Authorization": f"Bearer {credential.get_token(SCOPE).token}",
        "Content-Type": "application/json",
    }

    # A client-chosen id, never issued by the platform. That it works at all is
    # the first half of the finding.
    victim_id = f"victim{uuid.uuid4().hex}"
    control_id = f"control{uuid.uuid4().hex}"

    def say(session_id: str, message: str) -> str:
        resp = client.post(
            endpoint,
            headers=auth,
            json={"input": message, "store": True, "agent_session_id": session_id},
        )
        resp.raise_for_status()
        return text_of(resp.json()).strip()

    with httpx.Client(timeout=240.0) as client:
        stored = say(victim_id, f"Add {SECRET_VALUE} to my trip")
        # No previous_response_id: a genuinely separate conversation that knows
        # nothing except the session id.
        leaked = say(victim_id, "What is in my trip?")
        control = say(control_id, "What is in my trip?")

    return {
        "accepted_client_chosen_id": True,
        "victim_first_turn": stored,
        "read_with_id_only": leaked,
        "control_with_other_id": control,
        "state_leaked": SECRET_VALUE.lower() in leaked.lower(),
        "control_clean": SECRET_VALUE.lower() not in control.lower(),
    }


def probe_router(url: str) -> dict:
    """Can user B use a handle the router issued to user A?"""
    with httpx.Client(timeout=240.0) as client:
        alice = {"x-user-id": f"alice-{uuid.uuid4().hex[:8]}"}
        mallory = {"x-user-id": f"mallory-{uuid.uuid4().hex[:8]}"}

        first = client.post(
            f"{url}/chat",
            headers=alice,
            json={"session_id": "iso-alice", "message": f"Add {SECRET_VALUE} to my trip"},
        )
        first.raise_for_status()
        handle = first.json()["session_handle"]

        # 1. Alice replays her own handle: must work.
        legit = client.post(
            f"{url}/chat",
            headers=alice,
            json={
                "session_id": "iso-alice",
                "message": "What is in my trip?",
                "session_handle": handle,
            },
        )

        # 2. Mallory replays Alice's handle verbatim: must be refused.
        stolen = client.post(
            f"{url}/chat",
            headers=mallory,
            json={
                "session_id": "iso-mallory",
                "message": "What is in my trip?",
                "session_handle": handle,
            },
        )

        # 3. Mallory supplies a raw platform-shaped id: must be refused too,
        #    because the router only accepts signed handles.
        raw = client.post(
            f"{url}/chat",
            headers=mallory,
            json={
                "session_id": "iso-mallory",
                "message": "What is in my trip?",
                "session_handle": "a" * 64,
            },
        )

    legit_text = legit.json().get("reply", "") if legit.status_code == 200 else ""
    return {
        "owner_replay_status": legit.status_code,
        "owner_sees_own_state": SECRET_VALUE.lower() in legit_text.lower(),
        "stolen_handle_status": stolen.status_code,
        "raw_id_status": raw.status_code,
        "contained": stolen.status_code == 403 and raw.status_code == 403,
    }


def main() -> int:
    endpoint = os.environ.get("HOSTED_AGENT_ENDPOINT", "").strip()
    router = os.environ.get("HYBRID_URL", "").strip().rstrip("/")
    if not endpoint or not router:
        print("HOSTED_AGENT_ENDPOINT and HYBRID_URL must be set. Source .env.lab first.")
        return 2

    print("=== Part 1: the hosted runtime, called directly ===")
    runtime = probe_runtime(endpoint)
    print(f"  wrote to victim session : {runtime['victim_first_turn']}")
    print(f"  read back with id only  : {runtime['read_with_id_only']}")
    print(f"  control, different id   : {runtime['control_with_other_id']}")
    print(
        f"  -> session id is {'sufficient' if runtime['state_leaked'] else 'NOT sufficient'} "
        f"to read the sandbox; control {'clean' if runtime['control_clean'] else 'DIRTY'}"
    )

    print("\n=== Part 2: the same attack through the hybrid router ===")
    routed = probe_router(router)
    print(f"  owner replays own handle   : HTTP {routed['owner_replay_status']} "
          f"(sees own state: {routed['owner_sees_own_state']})")
    print(f"  other user replays handle  : HTTP {routed['stolen_handle_status']}")
    print(f"  other user sends raw id    : HTTP {routed['raw_id_status']}")
    print(f"  -> {'CONTAINED' if routed['contained'] else 'NOT CONTAINED'}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"06_session_isolation_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "experiment": "session_isolation",
                "utc": stamp,
                "runtime_direct": runtime,
                "through_router": routed,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {path}")

    ok = routed["contained"] and routed["owner_sees_own_state"]
    print("\nRESULT:", "PASS - the router is the isolation boundary" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
