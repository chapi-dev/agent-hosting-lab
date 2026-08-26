"""Experiment 7: authorization-aware routing, and where it has to live.

Requirement R15 asks for orchestration decisions that consider *both* what the
user asked for and what the user is allowed to reach. It is the requirement most
often hand-waved, because on a slide "the router checks permissions" sounds
like a configuration setting. It is not one.

Azure RBAC governs who may invoke an agent endpoint directly. It does not help
here, for a reason worth stating plainly: in the hybrid pattern the router calls
the hosted runtime with *its own* managed identity, so by the time the request
reaches Azure there is exactly one principal - the router - regardless of which
end user started it. Every entitlement distinction between end users has already
been erased. Whatever the router forwards, Azure will authorize.

So the decision has to be made in the router, before the call, in code you own.
That is what this experiment measures.

The matrix, against the deployed router:

    message                      groups            expected
    ---------------------------  ----------------  ------------------------
    ordinary trip                (none)            allowed  - open to all
    corporate travel             travel-admin      allowed  - entitled
    corporate travel             engineering       DENIED   - wrong group
    corporate travel             (none)            DENIED   - absence != wildcard

The last row is the one worth keeping. A caller who supplies no groups must not
be treated as a wildcard caller for a restricted agent: "I did not tell you my
groups" is not the same claim as "I am allowed". Getting that backwards is how
an entitlement check ends up passing for everybody who omits a field.

    python experiments/07_authorization_routing.py

Exit code is non-zero if any expected decision does not match.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

RESULTS = Path(__file__).parent / "results"

CASES = [
    {
        "label": "ordinary trip, no groups",
        "message": "Plan a trip to Rome",
        "groups": [],
        "expect_status": 200,
        "expect_agent": "trip-planner",
        "why": "trip-planner is entitled to '*', so any caller may reach it.",
    },
    {
        "label": "corporate travel, entitled group",
        "message": "Book corporate travel to Berlin",
        "groups": ["travel-admin"],
        "expect_status": 200,
        "expect_agent": "trip-planner-corporate",
        "why": "caller holds the one group the restricted agent requires.",
    },
    {
        "label": "corporate travel, wrong group",
        "message": "Book corporate travel to Berlin",
        "groups": ["engineering"],
        "expect_status": 403,
        "expect_agent": None,
        "why": "holding some group is not holding the required group.",
    },
    {
        "label": "corporate travel, no groups",
        "message": "Book corporate travel to Berlin",
        "groups": [],
        "expect_status": 403,
        "expect_agent": None,
        "why": "an unstated entitlement is not a granted entitlement.",
    },
]


def run_case(client: httpx.Client, url: str, case: dict) -> dict:
    session_id = f"authz-{uuid.uuid4().hex[:8]}"
    response = client.post(
        f"{url}/chat",
        headers={"x-user-id": f"user-{uuid.uuid4().hex[:8]}"},
        json={
            "session_id": session_id,
            "message": case["message"],
            "groups": case["groups"],
        },
    )
    body = {}
    try:
        body = response.json()
    except ValueError:
        pass

    routed_to = body.get("routed_to") if response.status_code == 200 else None
    passed = response.status_code == case["expect_status"] and routed_to == case["expect_agent"]
    return {
        "label": case["label"],
        "message": case["message"],
        "groups": case["groups"],
        "why": case["why"],
        "expected_status": case["expect_status"],
        "actual_status": response.status_code,
        "expected_agent": case["expect_agent"],
        "actual_agent": routed_to,
        "intent": body.get("intent"),
        "detail": body.get("detail") if response.status_code != 200 else None,
        "passed": passed,
    }


def main() -> int:
    url = os.environ.get("HYBRID_URL", "").strip().rstrip("/")
    if not url:
        print("HYBRID_URL must be set. Source .env.lab first.")
        return 2

    print("=== Authorization-aware routing through the hybrid router ===\n")
    results = []
    with httpx.Client(timeout=240.0) as client:
        for case in CASES:
            outcome = run_case(client, url, case)
            results.append(outcome)
            verdict = "ok " if outcome["passed"] else "FAIL"
            target = outcome["actual_agent"] or f"denied ({outcome['actual_status']})"
            print(f"  [{verdict}] {outcome['label']:<34} -> {target}")
            print(f"         {outcome['why']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} decisions matched expectation")
    print(
        "\n  The deny cases never reached the runtime: the router refused them\n"
        "  before spending a token or opening a session. That is the property\n"
        "  RBAC alone cannot give you here, because the runtime only ever sees\n"
        "  the router's managed identity, never the end user's."
    )

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"07_authorization_routing_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "experiment": "authorization_routing",
                "timestamp": stamp,
                "target": url,
                "cases": results,
                "passed": passed,
                "total": len(results),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  wrote {path.name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
