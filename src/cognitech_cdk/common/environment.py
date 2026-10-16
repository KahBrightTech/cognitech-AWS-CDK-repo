"""Loads `deployments/<env>/env.yaml` into typed objects.

Each stack gets its own props block in the YAML, so adding a stack means adding
a key here and a dataclass next to its construct.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from cognitech_cdk.common.config import Common
from cognitech_cdk.constructs.iam import IamProps
from cognitech_cdk.constructs.network import NetworkProps
from cognitech_cdk.constructs.security import SecurityGroupProps

DEPLOYMENTS_DIR = Path(__file__).resolve().parents[3] / "deployments"
ENV_FILE = "env.yaml"


@dataclass(frozen=True)
class Environment:
    name: str
    account_id: str
    region: str
    common: Common
    network: NetworkProps
    security_groups: tuple[SecurityGroupProps, ...] = ()
    iam: IamProps = IamProps()

    @property
    def cdk_env(self) -> Mapping[str, str]:
        return {"account": self.account_id, "region": self.region}


def load_environment(env_name: str, root: Path = DEPLOYMENTS_DIR) -> Environment:
    path = root / env_name / ENV_FILE
    if not path.is_file():
        known = ", ".join(list_environments(root)) or "none"
        raise FileNotFoundError(
            f"No config for '{env_name}'. Expected {path}. Known environments: {known}."
        )

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    return Environment(
        name=env_name,
        account_id=str(raw["account_id"]),
        region=str(raw["region"]),
        common=Common.from_dict(raw.get("common") or {}, env_name),
        network=NetworkProps.from_dict(raw["network"]),
        security_groups=SecurityGroupProps.load_all(raw.get("security_groups")),
        # External policy paths are project-root-relative, alongside
        # deployments/, rather than environment-folder-relative.
        iam=IamProps.from_dict(raw.get("iam"), base_dir=root.parent),
    )


def list_environments(root: Path = DEPLOYMENTS_DIR) -> list[str]:
    """Environment names found under `deployments/`."""
    if not root.is_dir():
        return []
    return sorted(path.parent.name for path in root.glob(f"*/{ENV_FILE}"))
