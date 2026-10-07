#!/usr/bin/env python3
"""Print the JSON list of environments a workflow should deploy.

Used as the matrix source in `.github/workflows/cdk-deploy.yml`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cognitech_cdk.settings import environments  # noqa: E402

# A change to any of these affects every environment.
SHARED_PATHS = ("src/", "app.py", "cdk.json", "requirements", "scripts/")


def changed_files(base: str, head: str) -> list[str]:
    diff = subprocess.run(
        ["git", "diff", "--name-only", base, head],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in diff.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="git ref to diff against; omit to select all")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--only", help="force a single environment, or 'all'")
    args = parser.parse_args()

    everything = environments()

    if args.only and args.only != "all":
        selected = [args.only] if args.only in everything else []
    elif args.only == "all" or not args.base:
        selected = everything
    else:
        changed = changed_files(args.base, args.head)
        if any(path.startswith(SHARED_PATHS) for path in changed):
            selected = everything
        else:
            selected = [
                env
                for env in everything
                if any(path.startswith(f"deployments/{env}/") for path in changed)
            ]

    print(json.dumps(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
