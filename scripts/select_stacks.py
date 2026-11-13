#!/usr/bin/env python3
"""Select workflow targets from the reviewed assembly without crossing exclusions."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


def select_stacks(
    manifest: Mapping[str, Any], requested: str, excluded: str, action: str = "diff",
) -> tuple[list[str], list[str]]:
    artifacts = manifest["artifacts"]
    stacks = {
        key: artifact["properties"].get("stackName", key)
        for key, artifact in artifacts.items()
        if artifact["type"] == "aws:cloudformation:stack"
    }
    if not stacks:
        raise ValueError("The synthesized assembly contains no deployment stacks.")

    def resolve(token: str, *, allow_suffix: bool) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", token):
            raise ValueError(f"Invalid stack name '{token}'.")
        exact = [key for key, name in stacks.items() if token in (key, name)]
        matches = exact or [
            key for key, name in stacks.items()
            if allow_suffix and name.endswith(f"-{token}")
        ]
        if len(matches) != 1:
            reason = "ambiguous" if matches else "unknown"
            raise ValueError(
                f"Stack '{token}' is {reason}. Use a full stack name. "
                f"Available: {', '.join(stacks.values())}."
            )
        return matches[0]

    requested = requested.strip() or "all"
    tokens = (
        [] if excluded.strip() == "none"
        else [token for token in re.split(r"[,\s]+", excluded.strip()) if token]
    )
    if tokens and requested != "all":
        raise ValueError("exclude can only be used with stacks=all.")
    omitted = list(dict.fromkeys(resolve(token, allow_suffix=True) for token in tokens))
    selected = (
        [key for key in stacks if key not in omitted]
        if requested == "all" else [resolve(requested, allow_suffix=False)]
    )
    if not selected:
        raise ValueError("Exclusions leave no stacks to act on.")

    def dependencies(key: str) -> set[str]:
        visited: set[str] = set()
        pending = list(artifacts[key].get("dependencies", []))
        while pending:
            dependency = pending.pop()
            if dependency in visited:
                continue
            visited.add(dependency)
            pending.extend(artifacts[dependency].get("dependencies", []))
        return visited

    # Deploy/diff expands prerequisites; destroy expands dependent stacks instead.
    sources, forbidden = (omitted, selected) if action == "destroy" else (selected, omitted)
    for source in sources:
        conflicts = dependencies(source).intersection(forbidden)
        if conflicts:
            names = ", ".join(stacks[key] for key in sorted(conflicts))
            raise ValueError(
                f"Cannot {action} this selection: '{stacks[source]}' depends on {names}, "
                "which crosses the exclusion boundary. Change the exclusion list."
            )
    return selected, omitted


def main() -> int:
    try:
        manifest = json.loads(Path("cdk.out/manifest.json").read_text(encoding="utf-8"))
        selected, omitted = select_stacks(
            manifest, os.environ.get("REQUESTED", "all"),
            os.environ.get("EXCLUDED", ""), os.environ.get("ACTION") or "diff",
        )
    except (OSError, ValueError) as error:
        print(f"::error::{error}")
        return 1

    requested = os.environ.get("REQUESTED", "").strip() or "all"
    targets = (
        " ".join([*selected, "--exclusively"]) if omitted
        else "--all" if requested == "all" else selected[0]
    )
    summary = (
        f"{len(selected)} stack(s); excluded: {', '.join(omitted)}"
        if omitted else f"{requested} ({len(selected)} stack(s))"
    )
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        print(f"TARGETS={targets}\nSELECTED={summary}", file=output)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        print(f"targets={targets}", file=output)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        print("### Selected stacks\n", file=output)
        for stack in selected:
            print(f"- `{stack}`", file=output)
        if omitted:
            print("\n### Excluded stacks\n", file=output)
            for stack in omitted:
                print(f"- `{stack}`", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
