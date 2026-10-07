"""Typed configuration loaded from `deployments/<env>/env.yaml`.

This is the CDK equivalent of Terraform `*.tfvars` plus `variables.tf`: the YAML
holds the values, the dataclasses below are the schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENTS_DIR = REPO_ROOT / "deployments"
ENV_FILE_NAME = "env.yaml"

# Every resource must carry these. `Name` is excluded: it is computed per resource.
REQUIRED_TAG_KEYS = ("Build-method", "Environment", "ManagedBy", "Owner", "Compliance")


@dataclass(frozen=True)
class CommonProps:
    """Equivalent of the `common` object variable used by every Terraform module."""

    account_name: str
    region_prefix: str
    tags: Mapping[str, str]

    def resource_name(self, *parts: str) -> str:
        """`{account_name}-{region_prefix}-{parts...}`, e.g. `int-production-use1-uat-vpc`."""
        return "-".join([self.account_name, self.region_prefix, *[p for p in parts if p]])


@dataclass(frozen=True)
class SubnetSpec:
    availability_zone: str
    cidr_block: str


@dataclass(frozen=True)
class SubnetGroupSpec:
    name: str
    subnets: Sequence[SubnetSpec]


@dataclass(frozen=True)
class NetworkSpec:
    name: str
    cidr_block: str
    nat_gateways: int
    public_subnet_groups: Sequence[SubnetGroupSpec]
    private_subnet_groups: Sequence[SubnetGroupSpec]


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    account_id: str
    region: str
    deploy_order: int
    common: CommonProps
    networks: Sequence[NetworkSpec]


def _subnet_groups(raw: Sequence[Mapping[str, Any]] | None) -> tuple[SubnetGroupSpec, ...]:
    return tuple(
        SubnetGroupSpec(
            name=group["name"],
            subnets=tuple(
                SubnetSpec(
                    availability_zone=subnet["availability_zone"],
                    cidr_block=subnet["cidr_block"],
                )
                for subnet in group["subnets"]
            ),
        )
        for group in raw or []
    )


def _common_tags(raw: Mapping[str, Any]) -> dict[str, str]:
    tags = {key: str(value) for key, value in raw.items()}

    if "Name" in tags:
        raise ValueError(
            "'Name' must not be set in common.tags; it is derived per resource from "
            "CommonProps.resource_name()."
        )

    missing = [key for key in REQUIRED_TAG_KEYS if not tags.get(key)]
    if missing:
        raise ValueError(f"common.tags is missing required keys: {', '.join(missing)}")

    return tags


def load_environment(
    env_name: str, deployments_dir: Path = DEPLOYMENTS_DIR
) -> EnvironmentConfig:
    config_file = deployments_dir / env_name / ENV_FILE_NAME
    if not config_file.is_file():
        known = ", ".join(available_environments(deployments_dir)) or "none"
        raise FileNotFoundError(
            f"No config for environment '{env_name}'. Expected {config_file}. Known: {known}."
        )

    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    common = CommonProps(
        account_name=raw["common"]["account_name"],
        region_prefix=raw["common"]["region_prefix"],
        tags=_common_tags(raw["common"].get("tags", {})),
    )

    networks = tuple(
        NetworkSpec(
            name=network["name"],
            cidr_block=network["cidr_block"],
            nat_gateways=int(network.get("nat_gateways", 0)),
            public_subnet_groups=_subnet_groups(network.get("public_subnet_groups")),
            private_subnet_groups=_subnet_groups(network.get("private_subnet_groups")),
        )
        for network in raw.get("networks", [])
    )

    return EnvironmentConfig(
        name=env_name,
        account_id=str(raw["account_id"]),
        region=raw["region"],
        deploy_order=int(raw.get("deploy_order", 100)),
        common=common,
        networks=networks,
    )


def available_environments(deployments_dir: Path = DEPLOYMENTS_DIR) -> list[str]:
    """Environment names found under `deployments/`, in promotion order."""
    if not deployments_dir.is_dir():
        return []

    names = [
        path.parent.name
        for path in sorted(deployments_dir.glob(f"*/{ENV_FILE_NAME}"))
    ]
    return sorted(
        names,
        key=lambda name: (load_environment(name, deployments_dir).deploy_order, name),
    )
