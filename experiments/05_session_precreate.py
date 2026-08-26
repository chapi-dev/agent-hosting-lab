"""Experiment 5: pre-create the session instead of pre-warming with a dummy turn.

Experiment 4 hides the cold start by sending a throwaway message when the chat
window opens. It works - about 70% of the first-turn latency disappears - but it costs
a model call and leaves a junk turn in the conversation.

The runtime exposes something better: POST {endpoint}/sessions provisions a
sandbox and returns its id without running the agent. `azd ai agent sessions
create` is the CLI wrapper over it.

The catch, and the reason this experiment exists, is *how* you then attach to
that session. The lab found two different attachment mechanisms with different
rules, and picking the wrong one silently discards the pre-created sandbox:

    header  x-agent-session-id    only honoured alongside previous_response_id.
                                  Sent alone on turn 1 - which is the only time
                                  it matters for pre-warming - it is ignored and
                                  the runtime allocates a fresh sandbox. You pay
                                  the full cold start and the pre-creation was
                                  wasted.

    body    agent_session_id      honoured on its own, including on turn 1.
                                  This is the one that makes pre-creation work.

That distinction is not obvious from either name, and the failure is silent: you
get a correct answer, slowly, from a sandbox you did not intend to use.

This experiment measures both, so the difference is a number rather than a claim.

Usage:
    python experiments/05_session_precreate.py --rounds 3
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

SCOPE = "https://ai.azure.com/.default"
RESULTS = Path(__file__).parent / "results"


def sessions_url(responses_endpoint: str) -> str:
    """Derives the sessions URL from the responses endpoint.

    The responses endpoint looks like
        .../agents/<name>/endpoint/protocols/openai/responses?api-version=v1
    and sessions hangs off the agent endpoint root, not off the protocol path:
        .../agents/<name>/endpoint/sessions?api-version=v1
    Posting to .../protocols/openai/sessions returns 404.
    """
    return responses_endpoint.split("/protocols/")[0] + "/sessions?api-version=v1"


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


def run_round(client: httpx.Client, endpoint: str, auth: dict, attach: str) -> dict:
    """One conversation against a pre-created session.

    `attach` selects the mechanism under test: "body" or "header".
    """
    started = time.perf_counter()
    created = client.post(sessions_url(endpoint), headers=auth, json={})
    created.raise_for_status()
    session_id = created.json()["agent_session_id"]
    create_ms = int((time.perf_counter() - started) * 1000)

    def send(message: str, previous_id: str | None) -> tuple[httpx.Response, int]:
        payload: dict = {"input": message, "store": True}
        headers = dict(auth)
        if attach == "body":
            payload["agent_session_id"] = session_id
        else:
            headers["x-agent-session-id"] = session_id
        if previous_id:
            payload["previous_response_id"] = previous_id
        begin = time.perf_counter()
        resp = client.post(endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        return resp, int((time.perf_counter() - begin) * 1000)

    r1, first_ms = send("Add Paris to my trip", None)
    b1 = r1.json()
    returned = r1.headers.get("x-agent-session-id") or b1.get("agent_session_id")

    r2, second_ms = send("What is in my trip?", b1.get("id"))
    kept = "paris" in text_of(r2.json()).lower()

    return {
        "attach": attach,
        "create_ms": create_ms,
        "first_turn_ms": first_ms,
        "second_turn_ms": second_ms,
        "session_reused": returned == session_id,
        "state_kept": kept,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    endpoint = os.environ.get("HOSTED_AGENT_ENDPOINT", "").strip()
    if not endpoint:
        print("HOSTED_AGENT_ENDPOINT not set. Source .env.lab first.")
        return 2

    auth = {
        "Authorization": f"Bearer {DefaultAzureCredential().get_token(SCOPE).token}",
        "Content-Type": "application/json",
    }

    results: dict = {}
    with httpx.Client(timeout=240.0) as client:
        for attach in ("body", "header"):
            print(f"\n=== attach via {attach} ===")
            rounds = []
            for index in range(args.rounds):
                try:
                    row = run_round(client, endpoint, auth, attach)
                except Exception as exc:
                    print(f"  round {index + 1}: ERROR {exc}")
                    continue
                rounds.append(row)
                print(
                    f"  round {index + 1}: create={row['create_ms']}ms "
                    f"first={row['first_turn_ms']}ms second={row['second_turn_ms']}ms "
                    f"| reused={row['session_reused']} state_kept={row['state_kept']}"
                )
            if rounds:
                results[attach] = {
                    "rounds": rounds,
                    "create_ms_median": round(statistics.median(r["create_ms"] for r in rounds)),
                    "first_turn_ms_median": round(
                        statistics.median(r["first_turn_ms"] for r in rounds)
                    ),
                    "second_turn_ms_median": round(
                        statistics.median(r["second_turn_ms"] for r in rounds)
                    ),
                    "session_reused_rate": sum(r["session_reused"] for r in rounds) / len(rounds),
                    "state_kept_rate": sum(r["state_kept"] for r in rounds) / len(rounds),
                }

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"05_session_precreate_{stamp}.json"
    path.write_text(
        json.dumps({"experiment": "session_precreate", "utc": stamp, "results": results}, indent=2),
        encoding="utf-8",
    )

    print(f"\nwrote {path}\n")
    print(f"{'attach':<10}{'create':>10}{'first turn':>13}{'reused':>10}{'state kept':>13}")
    for attach, data in results.items():
        print(
            f"{attach:<10}{data['create_ms_median']:>8}ms"
            f"{data['first_turn_ms_median']:>11}ms"
            f"{data['session_reused_rate'] * 100:>9.0f}%"
            f"{data['state_kept_rate'] * 100:>12.0f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
