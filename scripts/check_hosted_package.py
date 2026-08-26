"""Verifies that the hosted agent package is self-contained.

A hosted agent deploys a *directory*. Whatever the entry point imports has to
live inside that directory: there is no editable install, no PYTHONPATH, and no
site-packages you control. The failure mode is unpleasant because it only shows
up after a remote build and a deploy, as a ModuleNotFoundError in a log you have
to go looking for.

So we check it here instead, in a second, with no Azure involved:

    python scripts/check_hosted_package.py

Every absolute import in src/hosted must resolve either to a third-party package
declared in requirements.txt or to a directory that is physically present inside
src/hosted. Run scripts/prepare-hosted.ps1 first - it is what copies
src/agent_core into place.
"""

from __future__ import annotations

import ast
import pathlib
import sys

HOSTED = pathlib.Path(__file__).resolve().parents[1] / "src" / "hosted"

# Packages that must be vendored into the deploy directory rather than installed.
# agent_core is shared source, not a published distribution, so pip cannot help.
LOCAL_PACKAGES = {"agent_core"}


def main() -> int:
    if not HOSTED.is_dir():
        print(f"not found: {HOSTED}", file=sys.stderr)
        return 1

    missing: list[str] = []
    checked = 0

    for path in sorted(HOSTED.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        checked += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative import, resolves within the package
                    continue
                root = (node.module or "").split(".")[0]
            elif isinstance(node, ast.Import):
                root = node.names[0].name.split(".")[0]
            else:
                continue

            if root in LOCAL_PACKAGES and not (HOSTED / root).is_dir():
                rel = path.relative_to(HOSTED)
                missing.append(f"{rel} imports '{root}' but src/hosted/{root}/ does not exist")

    if missing:
        print("Hosted package is NOT self-contained:", file=sys.stderr)
        for line in dict.fromkeys(missing):
            print(f"  - {line}", file=sys.stderr)
        print("\nRun ./scripts/prepare-hosted.ps1 before deploying.", file=sys.stderr)
        return 1

    print(f"hosted package is self-contained ({checked} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
