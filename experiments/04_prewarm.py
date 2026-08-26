"""Experiment 4: can pre-warming hide the hosted cold start?

Experiment 2 established the one real disadvantage of hosted agents: the first
turn of a new session costs roughly eight seconds more than a self-hosted
container that is already warm. That is a genuine problem and this lab is not
going to argue it away.

But it is a problem with a specific shape. The penalty is paid *per session*,
not per turn, and it is paid when the session is created - not when the user
sends a message. Those are different moments, and in a chat UI they are usually
separated by several seconds of the user reading the greeting and typing.

So the question is not "how do we make sandbox provisioning faster". It is
"can we start the sandbox before the user has finished typing".

Method, per target:

  cold      Create a session and immediately send the real question. Measure it.
            This is what a naive client does, and it is what Experiment 2 measured.

  prewarmed Create the session with a throwaway priming request at the moment the
            chat window opens. Do not measure that - the user is not waiting for
            it, they are reading the greeting. Sleep for --think seconds to
            simulate them typing. Then send the real question and measure only
            that.

The prewarmed number is the honest measure of what the user experiences, because
the priming request happens during time the user was going to spend anyway.

This also tests something worth knowing independently: whether the sandbox
survives an idle gap. If pre-warming worked only for instantaneous follow-ups it
would be useless, because real users pause.

Usage:
    python experiments/04_prewarm.py --rounds 3 --think 8
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

PRIMER = "hello"
QUESTION = "Add Paris to my trip"
SCOPE = "https://ai.azure.com/.default"
RESULTS = Path(__file__).parent / "results"


class HostedSession:
    """Minimal client that carries both session handles.

    Both are required. `previous_response_id` continues the conversation and
    `x-agent-session-id` reattaches the sandbox, and the header is only honoured
    alongside the body field. Sending one without the other silently allocates a
    new sandbox, which would make every turn look like a cold start and would
    make this experiment report the opposite of the truth.
    """

    def __init__(self, endpoint: str, client: httpx.Client, token: str):
        self.endpoint = endpoint
        self.client = client
        self.token = token
        self.previous_id: str | None = None
        self.session_id: str | None = None

    def send(self, message: str) -> int:
        payload = {"input": message, "store": True}
        if self.previous_id:
            payload["previous_response_id"] = self.previous_id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["x-agent-session-id"] = self.session_id
        started = time.perf_counter()
        resp = self.client.post(self.endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()
        self.previous_id = body.get("id")
        self.session_id = (
            resp.headers.get("x-agent-session-id")
            or body.get("agent_session_id")
            or self.session_id
        )
        return int((time.perf_counter() - started) * 1000)


class RouterSession:
    """Same two handles, carried through the hybrid router's JSON contract."""

    def __init__(self, url: str, client: httpx.Client):
        self.url = url
        self.client = client
        self.session_id = f"exp4-{uuid.uuid4().hex[:10]}"
        self.agent_session_id: str | None = None
        self.previous_response_id: str | None = None

    def send(self, message: str) -> int:
        payload = {"session_id": self.session_id, "message": message}
        if self.agent_session_id:
            payload["agent_session_id"] = self.agent_session_id
        if self.previous_response_id:
            payload["previous_response_id"] = self.previous_response_id
        started = time.perf_counter()
        resp = self.client.post(f"{self.url}/chat", json=payload)
        resp.raise_for_status()
        body = resp.json()
        self.agent_session_id = body.get("agent_session_id") or self.agent_session_id
        self.previous_response_id = (
            body.get("previous_response_id") or self.previous_response_id
        )
        return int((time.perf_counter() - started) * 1000)


class SelfHostedSession:
    """Included as a control.

    Self-hosted apps at minReplicas>=2 have nothing to pre-warm, so this should
    show no meaningful difference. If it did, the measurement would be picking up
    noise rather than sandbox provisioning.
    """

    def __init__(self, url: str, client: httpx.Client):
        self.url = url
        self.client = client
        self.session_id = f"exp4-{uuid.uuid4().hex[:10]}"

    def send(self, message: str) -> int:
        started = time.perf_counter()
        resp = self.client.post(
            f"{self.url}/chat", json={"session_id": self.session_id, "message": message}
        )
        resp.raise_for_status()
        return int((time.perf_counter() - started) * 1000)


def make_session(cfg: dict, client: httpx.Client, token: str | None):
    if cfg["kind"] == "hosted":
        return HostedSession(cfg["url"], client, token or "")
    if cfg["kind"] == "router":
        return RouterSession(cfg["url"], client)
    return SelfHostedSession(cfg["url"], client)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--think",
        type=float,
        default=8.0,
        help="seconds of simulated user typing between priming and the real question",
    )
    args = parser.parse_args()

    targets = load_targets()
    if not targets:
        print("No targets configured.")
        return 2

    token = None
    if any(cfg["kind"] == "hosted" for cfg in targets.values()):
        token = DefaultAzureCredential().get_token(SCOPE).token

    results = {}
    for name, cfg in targets.items():
        print(f"\n=== {name} ===")
        cold, prewarmed = [], []
        with httpx.Client(timeout=240.0) as client:
            for round_index in range(args.rounds):
                try:
                    session = make_session(cfg, client, token)
                    cold_ms = session.send(QUESTION)

                    session = make_session(cfg, client, token)
                    primer_ms = session.send(PRIMER)
                    time.sleep(args.think)
                    warm_ms = session.send(QUESTION)
                except Exception as exc:
                    print(f"  round {round_index + 1}: ERROR {exc}")
                    continue

                cold.append(cold_ms)
                prewarmed.append(warm_ms)
                print(
                    f"  round {round_index + 1}: cold={cold_ms}ms  "
                    f"prewarmed={warm_ms}ms  (priming call took {primer_ms}ms, hidden)"
                )

        if cold and prewarmed:
            cold_med = round(statistics.median(cold))
            warm_med = round(statistics.median(prewarmed))
            results[name] = {
                "kind": cfg["kind"],
                "rounds": len(cold),
                "think_seconds": args.think,
                "cold_ms": {"median": cold_med, "samples": cold},
                "prewarmed_ms": {"median": warm_med, "samples": prewarmed},
                "saved_ms": cold_med - warm_med,
                "saved_pct": round((cold_med - warm_med) / cold_med * 100, 1)
                if cold_med
                else 0.0,
            }

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"04_prewarm_{stamp}.json"
    path.write_text(
        json.dumps({"experiment": "prewarm", "utc": stamp, "results": results}, indent=2),
        encoding="utf-8",
    )

    print(f"\nwrote {path}\n")
    print(f"{'target':<24}{'cold':>10}{'prewarmed':>12}{'saved':>10}{'':>3}")
    for name, data in results.items():
        print(
            f"{name:<24}{data['cold_ms']['median']:>8}ms"
            f"{data['prewarmed_ms']['median']:>10}ms"
            f"{data['saved_ms']:>8}ms   ({data['saved_pct']}%)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
