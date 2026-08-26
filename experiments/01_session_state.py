"""Experiment 1: does session state survive across turns?

This is the experiment the lab was built for. Everything else is supporting
evidence.

The same three-turn conversation is run against every deployment:

    turn 1  "Add Paris to my trip"
    turn 2  "Add Rome"
    turn 3  "What is in my trip?"

A deployment passes if turn 3 reports both Paris and Rome. It fails if anything
is missing - and note *how* it fails: not with a stack trace, but with a fluent,
confident, wrong answer. That is what makes this class of bug expensive. Nothing
pages you. The user is simply told something untrue.

Usage:
    python experiments/01_session_state.py --all
    python experiments/01_session_state.py --target naive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

TURNS = ["Add Paris to my trip", "Add Rome", "What is in my trip?"]
EXPECTED = ["paris", "rome"]
SCOPE = "https://ai.azure.com/.default"
RESULTS = Path(__file__).parent / "results"


def selfhosted_conversation(base_url: str, session_id: str) -> list[dict]:
    """Runs the three turns against a self-hosted /chat endpoint."""
    out = []
    with httpx.Client(timeout=180.0) as client:
        for turn in TURNS:
            started = time.perf_counter()
            resp = client.post(
                f"{base_url}/chat", json={"session_id": session_id, "message": turn}
            )
            resp.raise_for_status()
            body = resp.json()
            out.append(
                {
                    "turn": turn,
                    "reply": body.get("reply", ""),
                    # The replica name is the whole point: when it changes
                    # between turns and state is on local disk, state is gone.
                    "replica": body.get("replica", ""),
                    "state_backend": body.get("state_backend", ""),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )
    return out


def hosted_conversation(endpoint: str, session_id: str) -> list[dict]:
    """Runs the three turns against a Foundry hosted agent over Responses.

    There are two different identifiers here and conflating them is the single
    easiest way to lose state on the hosted model:

      previous_response_id   continues the *conversation* - the message history
                             the model sees.
      x-agent-session-id     selects the *sandbox* - the VM whose $HOME holds
                             anything your code wrote to disk.

    Send only `previous_response_id` and the model still sees the transcript,
    but each turn may land in a fresh sandbox, so files written last turn are
    gone. The agent then answers from the transcript alone and sounds perfectly
    reasonable while being wrong. We reproduced exactly that before adding the
    header: turn 3 replied "Your trip is empty" after two successful adds.

    The response returns the id in the `X-Agent-Session-Id` header and in
    `agent_session_id` in the body. Capture it on the first turn and echo it
    back on every subsequent turn.
    """
    credential = DefaultAzureCredential()
    token = credential.get_token(SCOPE).token
    out = []
    previous_id = None
    agent_session_id = None

    with httpx.Client(timeout=180.0) as client:
        for turn in TURNS:
            payload: dict = {"input": turn, "store": True}
            if previous_id:
                payload["previous_response_id"] = previous_id
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            if agent_session_id:
                headers["x-agent-session-id"] = agent_session_id

            started = time.perf_counter()
            resp = client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
            previous_id = body.get("id")
            agent_session_id = (
                resp.headers.get("x-agent-session-id")
                or body.get("agent_session_id")
                or agent_session_id
            )
            out.append(
                {
                    "turn": turn,
                    "reply": extract_text(body),
                    "replica": "n/a (platform-managed session)",
                    "state_backend": "hosted-session ($HOME)",
                    "response_id": previous_id,
                    "agent_session_id": agent_session_id,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )
    return out


def router_conversation(base_url: str, session_id: str) -> list[dict]:
    """Runs the three turns through the hybrid router.

    The router is stateless, so the session handle for the hosted runtime comes
    back on each reply and the caller echoes it on the next request. That is the
    trade the hybrid pattern makes explicit: the orchestration layer stays
    disposable precisely because it refuses to hold state.
    """
    out = []
    agent_session_id = None
    previous_response_id = None
    with httpx.Client(timeout=240.0) as client:
        for turn in TURNS:
            payload = {"session_id": session_id, "message": turn}
            if agent_session_id:
                payload["agent_session_id"] = agent_session_id
            if previous_response_id:
                payload["previous_response_id"] = previous_response_id
            started = time.perf_counter()
            resp = client.post(f"{base_url}/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
            agent_session_id = body.get("agent_session_id") or agent_session_id
            previous_response_id = body.get("previous_response_id") or previous_response_id
            out.append(
                {
                    "turn": turn,
                    "reply": body.get("reply", ""),
                    "replica": body.get("replica", ""),
                    "state_backend": body.get("state_backend", ""),
                    "routed_to": body.get("routed_to", ""),
                    "agent_session_id": agent_session_id,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )
    return out


def extract_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str) and body["output_text"]:
        return body["output_text"].strip()
    chunks = []
    for item in body.get("output", []) or []:
        for part in item.get("content", []) or []:
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
            elif isinstance(text, dict) and isinstance(text.get("value"), str):
                chunks.append(text["value"])
    return "\n".join(chunks).strip()


def evaluate(transcript: list[dict]) -> dict:
    """Judges the final turn only. The earlier turns are context, not evidence."""
    final = (transcript[-1]["reply"] if transcript else "").lower()
    found = [city for city in EXPECTED if city in final]
    missing = [city for city in EXPECTED if city not in final]
    replicas = {t.get("replica") for t in transcript if t.get("replica")}
    return {
        "passed": not missing,
        "found": found,
        "missing": missing,
        "distinct_replicas": len(replicas),
        "replicas": sorted(replicas),
        "mean_latency_ms": round(
            sum(t["latency_ms"] for t in transcript) / max(len(transcript), 1)
        ),
    }


def run_target(name: str, cfg: dict) -> dict:
    session_id = f"exp1-{uuid.uuid4().hex[:10]}"
    print(f"\n=== {name} ===")
    print(f"session: {session_id}")
    try:
        if cfg["kind"] == "selfhosted":
            transcript = selfhosted_conversation(cfg["url"], session_id)
        elif cfg["kind"] == "hosted":
            transcript = hosted_conversation(cfg["url"], session_id)
        else:
            transcript = router_conversation(cfg["url"], session_id)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return {"target": name, "error": str(exc), "verdict": {"passed": False}}

    for entry in transcript:
        replica = entry.get("replica", "")
        tag = replica[-6:] if replica and "n/a" not in replica else replica
        print(f"  >> {entry['turn']}")
        print(f"  << {entry['reply']}   [{tag}] {entry['latency_ms']}ms")

    verdict = evaluate(transcript)
    print(f"  VERDICT: {'PASS' if verdict['passed'] else 'FAIL'}", end="")
    if verdict["missing"]:
        print(f" - lost {', '.join(verdict['missing'])}", end="")
    if verdict["distinct_replicas"] > 1:
        print(f" - conversation spanned {verdict['distinct_replicas']} replicas", end="")
    print()

    return {
        "target": name,
        "kind": cfg["kind"],
        "session_id": session_id,
        "transcript": transcript,
        "verdict": verdict,
    }


def load_targets() -> dict:
    naive = os.environ.get("NAIVE_URL", "")
    hardened = os.environ.get("HARDENED_URL", "")
    hybrid = os.environ.get("HYBRID_URL", "")
    hosted = os.environ.get("HOSTED_AGENT_ENDPOINT", "")
    targets = {}
    if naive:
        targets["selfhosted-naive"] = {"kind": "selfhosted", "url": naive}
    if hardened:
        targets["selfhosted-hardened"] = {"kind": "selfhosted", "url": hardened}
    if hosted:
        targets["hosted-agent"] = {"kind": "hosted", "url": hosted}
    if hybrid:
        targets["hybrid-router"] = {"kind": "router", "url": hybrid}
    return targets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="run one target only")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()

    targets = load_targets()
    if not targets:
        print("No targets configured. Set NAIVE_URL / HARDENED_URL / "
              "HOSTED_AGENT_ENDPOINT / HYBRID_URL.")
        return 2
    if args.target:
        targets = {k: v for k, v in targets.items() if args.target in k}

    results = []
    for _ in range(args.repeat):
        for name, cfg in targets.items():
            results.append(run_target(name, cfg))

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"01_session_state_{stamp}.json"
    path.write_text(
        json.dumps(
            {"experiment": "session_state", "utc": stamp, "results": results},
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nwrote {path}")
    print("\nSUMMARY")
    for r in results:
        verdict = r.get("verdict", {})
        status = "PASS" if verdict.get("passed") else "FAIL"
        note = ""
        if verdict.get("missing"):
            note = f"  (lost: {', '.join(verdict['missing'])})"
        elif r.get("error"):
            note = f"  ({r['error'][:60]})"
        print(f"  {r['target']:<22} {status}{note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
