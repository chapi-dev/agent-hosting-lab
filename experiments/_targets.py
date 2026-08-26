"""Shared target configuration for the experiments.

Every experiment reads the same four endpoints from the same four environment
variables, so a target is configured once and measured consistently. Source
`.env.lab` (written by `scripts/deploy.ps1`) before running anything here.
"""

from __future__ import annotations

import os


def load_targets() -> dict:
    """Returns the configured targets, skipping any that are not deployed."""
    candidates = [
        ("selfhosted-naive", "NAIVE_URL", "selfhosted"),
        ("selfhosted-hardened", "HARDENED_URL", "selfhosted"),
        ("hosted-agent", "HOSTED_AGENT_ENDPOINT", "hosted"),
        ("hybrid-router", "HYBRID_URL", "router"),
    ]
    targets = {}
    for name, env_var, kind in candidates:
        url = os.environ.get(env_var, "").strip()
        if url:
            targets[name] = {"kind": kind, "url": url}
    return targets
