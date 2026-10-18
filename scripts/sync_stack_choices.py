#!/usr/bin/env python3
"""Keep the stack dropdown in each deploy workflow in sync with the app.

GitHub builds the "Run workflow" form from the YAML on the default branch,
before any runner exists, so a `choice` input cannot be filled in while the job
runs. This script closes that gap: it asks `cdk list` what each environment
actually builds and writes the answer into the generated block of the matching
workflow. The dropdown is therefore detected from `app.py`, never typed.

    python scripts/sync_stack_choices.py           # rewrite what changed
    python scripts/sync_stack_choices.py --check   # fail if anything is stale

A workflow opts in by declaring the environment it deploys as a workflow- or job-level
`ENV:` and marking the lines this script owns:

        # BEGIN generated stack options -- scripts/sync_stack_choices.py
        options:
          - all
          - md-preproduction-use1-dev-network
        # END generated stack options

Workflows without those markers are left alone.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW_GLOB = "deploy-*.yml"

BEGIN = "# BEGIN generated stack options"
END = "# END generated stack options"

# The first choice is not a stack: it means "everything this environment builds".
ALL = "all"


def environment_of(text: str) -> str | None:
    """The environment a deploy workflow targets, taken from its `ENV:`."""
    data = yaml.safe_load(text) or {}

    for job in (data.get("jobs") or {}).values():
        env = (job or {}).get("env") or {}
        if "ENV" in env:
            return str(env["ENV"])

    env = data.get("env") or {}
    return str(env["ENV"]) if "ENV" in env else None


def cdk_stacks(env_name: str) -> list[str]:
    """Stack names `cdk list` reports for one environment."""
    cdk = shutil.which("cdk")
    if cdk is None:
        raise SystemExit(
            "The cdk CLI is not on PATH. Install it with: npm install -g aws-cdk@2"
        )

    result = subprocess.run(
        [cdk, "list"],
        cwd=REPO_ROOT,
        env={**os.environ, "ENV": env_name},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"`cdk list` failed for ENV={env_name}:\n{result.stderr}")

    stacks = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not stacks:
        raise SystemExit(f"`cdk list` reported no stacks for ENV={env_name}.")
    return stacks


def render(text: str, stacks: list[str]) -> str:
    """Return `text` with the generated block replaced by `stacks`."""
    lines = text.splitlines(keepends=True)

    begins = [i for i, line in enumerate(lines) if line.strip().startswith(BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.strip().startswith(END)]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise ValueError(f"expected exactly one {BEGIN} ... {END} block")

    start, stop = begins[0], ends[0]
    indent = " " * (len(lines[start]) - len(lines[start].lstrip()))

    block = [lines[start], f"{indent}options:\n"]
    block += [f"{indent}  - {name}\n" for name in [ALL, *stacks]]
    block.append(lines[stop])

    return "".join(lines[:start] + block + lines[stop + 1 :])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale dropdowns instead of rewriting them",
    )
    args = parser.parse_args(argv)

    workflows = sorted(WORKFLOWS.glob(WORKFLOW_GLOB))
    if not workflows:
        raise SystemExit(f"No {WORKFLOW_GLOB} found in {WORKFLOWS}.")

    stale = False
    for path in workflows:
        current = path.read_text()
        if BEGIN not in current:
            print(f"{path.name}: no generated block, skipped")
            continue

        env_name = environment_of(current)
        if env_name is None:
            raise SystemExit(f"{path.name}: no workflow- or job-level ENV, so the environment is unknown.")

        updated = render(current, cdk_stacks(env_name))
        if updated == current:
            print(f"{path.name}: up to date ({env_name})")
            continue

        stale = True
        if args.check:
            print(f"{path.name}: STALE ({env_name}) -- run scripts/sync_stack_choices.py")
        else:
            path.write_text(updated)
            print(f"{path.name}: updated ({env_name})")

    return 1 if (stale and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
