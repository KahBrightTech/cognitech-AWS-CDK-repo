"""Shared helpers for building throwaway environment files."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

COMMON = """account_id: "111122223333"
region: us-east-1
common:
  account_name: int-production
  region_prefix: use1
  global: false
  account_name_abr: intprod
  environment_abr: tst
  tags:
    Build-method: aws-cdk
    Environment: test
    ManagedBy: aws-cdk/repo
    Owner: someone@example.com
    Compliance: hippaa
"""


@pytest.fixture
def write_env(tmp_path):
    """Write `deployments/<name>/env.yaml` from a `network:` block.

    `security_groups` is the body of the optional list of the same name, left
    out entirely when not given.

    Returns the deployments root, for `load_environment(name, root=...)`.
    """

    def _write(
        network: str, *, security_groups: str | None = None, name: str = "test"
    ) -> Path:
        env_dir = tmp_path / name
        env_dir.mkdir(parents=True, exist_ok=True)

        body = f"network:\n{_block(network)}\n"
        if security_groups is not None:
            body += f"security_groups:\n{_block(security_groups)}\n"

        (env_dir / "env.yaml").write_text(f"{COMMON}{body}", encoding="utf-8")
        return tmp_path

    return _write


def _block(text: str) -> str:
    return textwrap.indent(textwrap.dedent(text).strip(), "  ")
