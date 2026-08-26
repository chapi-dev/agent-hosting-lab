"""Experiment 3: how much of this repository exists only because of hosting?

Latency you can feel and state loss you can reproduce. This experiment measures
something quieter and, over a platform's lifetime, more expensive: how much
code, configuration and infrastructure each hosting model obliges you to own.

It counts real files in this repository. Nothing is estimated.

What is counted, and why:

  agent logic        src/agent_core - shared by every deployment. This is the
                     part you would have to write no matter what. It is the
                     denominator for everything else.

  hosting code       the server, the Dockerfile, the router - code that exists
                     to run the agent rather than to be the agent.

  infrastructure     Bicep. Counted with comments and blank lines stripped, so
                     the numbers reflect declarations rather than the
                     explanatory prose this repository is full of.

The ratio is the finding. When the infrastructure needed to run an agent is
several times the size of the agent, the hosting decision has stopped being an
implementation detail.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(__file__).parent / "results"

BICEP_COMMENT = re.compile(r"^\s*(//|/\*|\*)")
PY_COMMENT = re.compile(r"^\s*#")


def count_lines(path: Path, comment_pattern: re.Pattern | None) -> int:
    """Counts non-blank, non-comment lines."""
    if not path.exists():
        return 0
    total = 0
    in_docstring = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if comment_pattern is PY_COMMENT:
            # Crude but adequate: this repository writes docstrings with triple
            # double-quotes on their own lines.
            if stripped.startswith('"""') or stripped.endswith('"""'):
                quote_count = stripped.count('"""')
                if quote_count == 1:
                    in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
        if comment_pattern and comment_pattern.match(line):
            continue
        total += 1
    return total


def count_tree(directory: Path, pattern: str, comment_pattern) -> tuple[int, list[str]]:
    total = 0
    files = []
    for path in sorted(directory.rglob(pattern)):
        if "__pycache__" in path.parts:
            continue
        lines = count_lines(path, comment_pattern)
        if lines:
            total += lines
            files.append(f"{path.relative_to(ROOT).as_posix()} ({lines})")
    return total, files


def main() -> int:
    src = ROOT / "src"
    infra = ROOT / "infra"

    shared, shared_files = count_tree(src / "agent_core", "*.py", PY_COMMENT)

    selfhosted_code = count_lines(src / "selfhosted" / "server.py", PY_COMMENT)
    router_code = count_lines(src / "selfhosted" / "router.py", PY_COMMENT)
    dockerfile = count_lines(src / "Dockerfile", BICEP_COMMENT)
    selfhosted_reqs = count_lines(src / "selfhosted" / "requirements.txt", PY_COMMENT)

    hosted_code = count_lines(src / "hosted" / "main.py", PY_COMMENT)
    hosted_reqs = count_lines(src / "hosted" / "requirements.txt", PY_COMMENT)
    azure_yaml = count_lines(ROOT / "azure.yaml", PY_COMMENT)

    infra_files = {
        "main.bicep": count_lines(infra / "main.bicep", BICEP_COMMENT),
        "apps.bicep": count_lines(infra / "apps.bicep", BICEP_COMMENT),
        "network.bicep": count_lines(infra / "network.bicep", BICEP_COMMENT),
        "environment-vnet.bicep": count_lines(
            infra / "environment-vnet.bicep", BICEP_COMMENT
        ),
    }

    # main.bicep is shared: the Foundry project and model deployment are needed
    # by both models. Attributing all of it to self-hosting would overstate the
    # case, and this lab is not interested in overstating the case.
    shared_infra = infra_files["main.bicep"]
    selfhosted_only_infra = (
        infra_files["apps.bicep"]
        + infra_files["network.bicep"]
        + infra_files["environment-vnet.bicep"]
    )

    selfhosted_total = (
        selfhosted_code + dockerfile + selfhosted_reqs + selfhosted_only_infra
    )
    hosted_total = hosted_code + hosted_reqs + azure_yaml

    report = {
        "shared_agent_logic": {"lines": shared, "files": shared_files},
        "self_hosted_only": {
            "server.py": selfhosted_code,
            "Dockerfile": dockerfile,
            "requirements.txt": selfhosted_reqs,
            "apps.bicep": infra_files["apps.bicep"],
            "network.bicep": infra_files["network.bicep"],
            "environment-vnet.bicep": infra_files["environment-vnet.bicep"],
            "total": selfhosted_total,
            "ratio_to_agent": round(selfhosted_total / shared, 2) if shared else None,
        },
        "hosted_only": {
            "main.py": hosted_code,
            "requirements.txt": hosted_reqs,
            "azure.yaml": azure_yaml,
            "total": hosted_total,
            "ratio_to_agent": round(hosted_total / shared, 2) if shared else None,
        },
        "shared_infrastructure": {
            "main.bicep": shared_infra,
            "note": "Foundry project, model deployment, observability, identity - "
                    "needed by both models.",
        },
        "hybrid_router": {
            "router.py": router_code,
            "note": "The price of the hybrid pattern: one small stateless "
                    "service you own, on top of a hosted runtime you do not.",
        },
        "multiplier": (
            round(selfhosted_total / hosted_total, 2) if hosted_total else None
        ),
    }

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"03_deployment_surface_{stamp}.json"
    path.write_text(
        json.dumps(
            {"experiment": "deployment_surface", "utc": stamp, "report": report},
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Shared agent logic (both models need this)")
    for entry in shared_files:
        print(f"  {entry}")
    print(f"  TOTAL {shared} lines\n")

    print("Self-hosted only")
    for key, value in report["self_hosted_only"].items():
        if key not in ("total", "ratio_to_agent"):
            print(f"  {key:<26}{value:>6}")
    print(f"  {'TOTAL':<26}{selfhosted_total:>6}  "
          f"({report['self_hosted_only']['ratio_to_agent']}x the agent itself)\n")

    print("Hosted only")
    for key, value in report["hosted_only"].items():
        if key not in ("total", "ratio_to_agent"):
            print(f"  {key:<26}{value:>6}")
    print(f"  {'TOTAL':<26}{hosted_total:>6}  "
          f"({report['hosted_only']['ratio_to_agent']}x the agent itself)\n")

    print(f"Self-hosting costs {report['multiplier']}x the supporting code of hosting.")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
