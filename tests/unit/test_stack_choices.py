"""The stack dropdown in each deploy workflow is generated, not typed.

`scripts/sync_stack_choices.py` writes it from `cdk list`. These tests check the
rewriting itself, and that what is committed still matches what the app builds,
so a stack added to `app.py` without re-running the script fails the PR instead
of showing a stale form.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "sync_stack_choices.py"

DEPLOY_WORKFLOWS = sorted(WORKFLOWS.glob("deploy-*.yml"))


def _load_script():
    spec = importlib.util.spec_from_file_location("sync_stack_choices", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_script()


WORKFLOW = """\
on:
  workflow_dispatch:
    inputs:
      stacks:
        type: choice
        # BEGIN generated stack options -- scripts/sync_stack_choices.py
        options:
          - all
          - stale-stack
        # END generated stack options
jobs:
  cdk:
    env:
      ENV: dev
"""


def _options(text: str) -> list[str]:
    """The stack names between the generated markers."""
    inside = text.split(sync.BEGIN)[1].split(sync.END)[0]
    return [
        line.strip().removeprefix("- ")
        for line in inside.splitlines()
        if line.strip().startswith("- ")
    ]


def _built_stacks(env_name: str, outdir: Path) -> set[str]:
    """What `app.py` builds, without needing the cdk CLI (see test_app.py)."""
    env = {k: v for k, v in os.environ.items() if k not in ("ENV", "CDK_CONTEXT_JSON")}
    env["JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION"] = "1"
    env["CDK_OUTDIR"] = str(outdir)
    env["ENV"] = env_name

    result = subprocess.run(
        [sys.executable, "app.py"], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    return {p.name.removesuffix(".template.json") for p in outdir.glob("*.template.json")}


def test_render_replaces_the_options_and_nothing_else():
    updated = sync.render(WORKFLOW, ["one", "two"])

    assert _options(updated) == ["all", "one", "two"]
    assert "stale-stack" not in updated
    assert "ENV: dev" in updated
    # The indentation has to survive, or the workflow stops being valid YAML.
    assert "        options:\n          - all\n" in updated


def test_render_is_stable_when_nothing_changed():
    once = sync.render(WORKFLOW, ["one"])

    assert sync.render(once, ["one"]) == once


def test_render_refuses_a_file_without_markers():
    with pytest.raises(ValueError):
        sync.render("on: push\n", ["one"])


def test_environment_comes_from_the_job_env():
    assert sync.environment_of(WORKFLOW) == "dev"


def test_environment_is_none_when_not_declared():
    assert sync.environment_of("jobs:\n  cdk:\n    runs-on: ubuntu-latest\n") is None


@pytest.mark.parametrize("path", DEPLOY_WORKFLOWS, ids=lambda p: p.name)
def test_committed_dropdown_matches_the_app(path, tmp_path):
    text = path.read_text()
    if sync.BEGIN not in text:
        pytest.skip(f"{path.name} has no generated stack options")

    env_name = sync.environment_of(text)
    assert env_name is not None, f"{path.name} does not say which environment it deploys"

    options = _options(text)
    assert options[0] == sync.ALL, "the first choice must be 'all'"
    assert set(options[1:]) == _built_stacks(env_name, tmp_path), (
        f"{path.name} is out of date. Run: python scripts/sync_stack_choices.py"
    )
