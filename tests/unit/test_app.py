"""The app entrypoint: how the environment name is resolved.

`app.py` is a script rather than a module, so these run it as a subprocess the
same way the `cdk` CLI does. The CLI passes `--context` through the
`CDK_CONTEXT_JSON` variable and the output directory through `CDK_OUTDIR`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cognitech_cdk.common.environment import load_environment

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(outdir: Path, *, env_var: str | None = None, context: str | None = None):
    env = {k: v for k, v in os.environ.items() if k not in ("ENV", "CDK_CONTEXT_JSON")}
    env["JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION"] = "1"
    env["CDK_OUTDIR"] = str(outdir)
    if env_var is not None:
        env["ENV"] = env_var
    if context is not None:
        # This is how `cdk --context env=<name>` reaches the app.
        env["CDK_CONTEXT_JSON"] = json.dumps({"env": context})

    return subprocess.run(
        [sys.executable, "app.py"], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )


def _stacks(outdir: Path) -> set[str]:
    return {p.name.removesuffix(".template.json") for p in outdir.glob("*.template.json")}


def _expected_stack(env_name: str) -> str:
    """Build the stack name from the config, so edits to env.yaml do not break these."""
    environment = load_environment(env_name)
    return environment.common.name(environment.network.name, "network")


def test_defaults_to_dev_when_nothing_is_set(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _stacks(tmp_path) == {_expected_stack("dev")}


def test_env_variable_selects_the_environment(tmp_path):
    result = _run(tmp_path, env_var="prod")

    assert result.returncode == 0, result.stderr
    assert _stacks(tmp_path) == {_expected_stack("prod")}


def test_context_overrides_the_variable(tmp_path):
    """--context is the more explicit signal, so it must win."""
    result = _run(tmp_path, env_var="prod", context="dev")

    assert result.returncode == 0, result.stderr
    assert _stacks(tmp_path) == {_expected_stack("dev")}


@pytest.mark.parametrize("kwargs", [{"env_var": "staging"}, {"context": "staging"}])
def test_unknown_environment_fails_with_a_helpful_message(tmp_path, kwargs):
    result = _run(tmp_path, **kwargs)

    assert result.returncode != 0
    assert "Unknown environment 'staging'" in result.stderr
    assert "dev, prod" in result.stderr
    assert "ENV" in result.stderr
