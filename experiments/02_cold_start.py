"""Experiment 2: what does the first request cost?

Cold start is where the two hosting models differ most visibly, and where the
comparison is most often made unfairly. This experiment measures the thing that
actually matters to a user - time to a complete answer on a conversation that
has just started - and separates it from the steady-state latency that follows.

Method:
  * For each target, start a brand new conversation and time turn 1.
  * Then send two more turns on the same conversation and time those.
  * Repeat N times with a fresh conversation each round.

Reading the results:
  * `first_turn_ms` includes whatever the platform had to do to get ready.
    For a hosted agent that is sandbox provisioning. For a self-hosted app with
    minReplicas >= 1 it is nothing, because you are paying to keep it warm.
  * `warm_turn_ms` is the honest steady-state comparison.
  * The gap between them is what you would be buying if you paid for warm
    replicas - and the self-hosted numbers here already include that purchase.

The self-hosted variants in this lab run minReplicas=2, so they never pay a cold
start and never stop billing. That asymmetry is the point, not a flaw in the
measurement: it is the trade being made.

Usage:
    python experiments/02_cold_start.py --rounds 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

sys.path.insert(0, str(Path(__file__).parent))

from _targets import load_targets  # noqa: E402

TURNS = ["Add Paris to my trip", "Add Rome", "What is in my trip?"]
SCOPE = "https://ai.azure.com/.default"
RESULTS = Path(__file__).parent / "results"


def time_selfhosted(url: str) -> list[int]:
    session_id = f"exp2-{uuid.uuid4().hex[:10]}"
    timings = []
    with httpx.Client(timeout=240.0) as client:
        for turn in TURNS:
            started = time.perf_counter()
            resp = client.post(
                f"{url}/chat", json={"session_id": session_id, "message": turn}
            )
            resp.raise_for_status()
            timings.append(int((time.perf_counter() - started) * 1000))
    return timings


def time_hosted(endpoint: str) -> list[int]:
    credential = DefaultAzureCredential()
    token = credential.get_token(SCOPE).token
    timings = []
    previous_id = None
    session_id = None
    with httpx.Client(timeout=240.0) as client:
        for turn in TURNS:
            payload = {"input": turn, "store": True}
            if previous_id:
                payload["previous_response_id"] = previous_id
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            if session_id:
                headers["x-agent-session-id"] = session_id
            started = time.perf_counter()
            resp = client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
            previous_id = body.get("id")
            session_id = (
                resp.headers.get("x-agent-session-id")
                or body.get("agent_session_id")
                or session_id
            )
            timings.append(int((time.perf_counter() - started) * 1000))
    return timings


def time_router(url: str) -> list[int]:
    """Times the hybrid router, propagating both session handles.

    Without this the router opens a new sandbox on every turn and every turn
    pays a cold start, which would make the hybrid pattern look far worse than
    it is. We measured exactly that by accident: a flat ~9.6s per turn instead
    of one cold start followed by warm turns. Worth knowing, because the same
    mistake in a real client produces the same flat latency in production.
    """
    session_id = f"exp2-{uuid.uuid4().hex[:10]}"
    timings = []
    session_handle = None
    previous_response_id = None
    headers = {"x-user-id": f"user-{session_id}"}
    with httpx.Client(timeout=240.0) as client:
        for turn in TURNS:
            payload = {"session_id": session_id, "message": turn}
            if session_handle:
                payload["session_handle"] = session_handle
            if previous_response_id:
                payload["previous_response_id"] = previous_response_id
            started = time.perf_counter()
            resp = client.post(f"{url}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
            session_handle = body.get("session_handle") or session_handle
            previous_response_id = body.get("previous_response_id") or previous_response_id
            timings.append(int((time.perf_counter() - started) * 1000))
    return timings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    targets = load_targets()
    if not targets:
        print("No targets configured.")
        return 2

    results = {}
    for name, cfg in targets.items():
        print(f"\n=== {name} ===")
        firsts, warms = [], []
        for round_index in range(args.rounds):
            try:
                if cfg["kind"] == "hosted":
                    timings = time_hosted(cfg["url"])
                elif cfg["kind"] == "router":
                    timings = time_router(cfg["url"])
                else:
                    timings = time_selfhosted(cfg["url"])
            except Exception as exc:
                print(f"  round {round_index + 1}: ERROR {exc}")
                continue
            firsts.append(timings[0])
            warms.extend(timings[1:])
            print(
                f"  round {round_index + 1}: first={timings[0]}ms "
                f"then={'/'.join(str(t) for t in timings[1:])}ms"
            )

        if firsts:
            results[name] = {
                "kind": cfg["kind"],
                "rounds": len(firsts),
                "first_turn_ms": {
                    "median": round(statistics.median(firsts)),
                    "min": min(firsts),
                    "max": max(firsts),
                    "samples": firsts,
                },
                "warm_turn_ms": {
                    "median": round(statistics.median(warms)) if warms else None,
                    "min": min(warms) if warms else None,
                    "max": max(warms) if warms else None,
                    "samples": warms,
                },
                "cold_start_penalty_ms": (
                    round(statistics.median(firsts) - statistics.median(warms))
                    if warms
                    else None
                ),
            }

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"02_cold_start_{stamp}.json"
    path.write_text(
        json.dumps({"experiment": "cold_start", "utc": stamp, "results": results}, indent=2),
        encoding="utf-8",
    )

    print(f"\nwrote {path}\n")
    print(f"{'target':<24}{'first (med)':>14}{'warm (med)':>14}{'penalty':>12}")
    for name, data in results.items():
        print(
            f"{name:<24}{data['first_turn_ms']['median']:>12}ms"
            f"{data['warm_turn_ms']['median']:>12}ms"
            f"{data['cold_start_penalty_ms']:>10}ms"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
