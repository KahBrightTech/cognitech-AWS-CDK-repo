"""Typed configuration loaded from `deployments/<env>/env.yaml`.

Subnet CIDRs are resolved here, so a `NetworkSpec` always carries concrete
per-availability-zone ranges regardless of whether the YAML listed them or asked
for a `cidr_mask` to be carved out of the VPC range.
"""

from __future__ import annotations

import ipaddress
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENTS_DIR = REPO_ROOT / "deployments"
ENV_FILE_NAME = "env.yaml"

# Every resource must carry these. `Name` is excluded: it is computed per resource.
REQUIRED_TAG_KEYS = ("Build-method", "Environment", "ManagedBy", "Owner", "Compliance")

SUBNET_TYPES = ("public", "private", "isolated")

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class CommonProps:
    """Values shared by every resource in an environment."""

    account_name: str
    region_prefix: str
    tags: Mapping[str, str]
    # Distinguishes these resources from the Terraform-managed ones. Set to "" to drop it.
    qualifier: str = ""

    def resource_name(self, *parts: str) -> str:
        """`{account_name}-{region_prefix}-{qualifier}-{parts...}`.

        e.g. `int-production-use1-cdk-uat-vpc`. Empty segments are skipped.
        """
        segments = (self.account_name, self.region_prefix, self.qualifier, *parts)
        return "-".join(segment for segment in segments if segment)


@dataclass(frozen=True)
class SubnetGroupSpec:
    """One tier of subnets, with one CIDR per availability zone."""

    name: str
    type: str
    cidrs: Sequence[str]
    # Public groups only: auto-assign a public IPv4 to instances launched here.
    map_public_ip_on_launch: bool = True


@dataclass(frozen=True)
class NetworkSpec:
    name: str
    cidr_block: str
    availability_zones: Sequence[str]
    nat_gateways: int
    subnet_groups: Sequence[SubnetGroupSpec]


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    account_id: str
    region: str
    deploy_order: int
    common: CommonProps
    networks: Sequence[NetworkSpec]


def _availability_zones(network: Mapping[str, Any], region: str) -> tuple[str, ...]:
    explicit = network.get("availability_zones")
    if explicit:
        return tuple(str(zone) for zone in explicit)

    count = int(network.get("az_count", 0))
    if count < 1:
        raise ValueError(
            f"Network '{network['name']}' must set availability_zones or az_count."
        )
    return tuple(f"{region}{letter}" for letter in string.ascii_lowercase[:count])


def _first_free(vpc: IpNetwork, prefix: int, taken: Sequence[IpNetwork]) -> IpNetwork:
    for candidate in vpc.subnets(new_prefix=prefix):
        if not any(candidate.overlaps(block) for block in taken):
            return candidate
    raise ValueError(f"No free /{prefix} block left in {vpc}.")


def _assert_no_overlap(blocks: Iterable[IpNetwork], network_name: str) -> None:
    seen: list[IpNetwork] = []
    for block in blocks:
        for other in seen:
            if block.overlaps(other):
                raise ValueError(
                    f"Network '{network_name}': subnet CIDRs {block} and {other} overlap."
                )
        seen.append(block)


def _resolve_cidrs(
    raw: Mapping[str, Any], azs: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """Return concrete CIDRs per group, carving any that only gave a `cidr_mask`."""
    network_name = raw["name"]
    vpc = ipaddress.ip_network(raw["cidr_block"])
    groups = raw.get("subnet_groups") or []

    explicit: dict[str, tuple[str, ...]] = {}
    taken: list[IpNetwork] = []

    for group in groups:
        cidrs = group.get("cidrs")
        if not cidrs:
            continue
        if group.get("cidr_mask"):
            raise ValueError(
                f"Subnet group '{group['name']}' sets both cidrs and cidr_mask; pick one."
            )
        blocks = [ipaddress.ip_network(cidr) for cidr in cidrs]
        if len(blocks) != len(azs):
            raise ValueError(
                f"Subnet group '{group['name']}' lists {len(blocks)} CIDRs but the network "
                f"uses {len(azs)} availability zones."
            )
        for block in blocks:
            if not block.subnet_of(vpc):
                raise ValueError(
                    f"Subnet CIDR {block} is outside the VPC range {vpc} "
                    f"in network '{network_name}'."
                )
        explicit[group["name"]] = tuple(str(block) for block in blocks)
        taken.extend(blocks)

    _assert_no_overlap(taken, network_name)

    resolved: dict[str, tuple[str, ...]] = {}
    for group in groups:
        if group["name"] in explicit:
            resolved[group["name"]] = explicit[group["name"]]
            continue
        prefix = int(group.get("cidr_mask", 24))
        carved = []
        for _ in azs:
            block = _first_free(vpc, prefix, taken)
            taken.append(block)
            carved.append(str(block))
        resolved[group["name"]] = tuple(carved)

    return resolved


def _subnet_groups(
    raw: Mapping[str, Any], cidrs: Mapping[str, tuple[str, ...]]
) -> tuple[SubnetGroupSpec, ...]:
    groups = []
    for group in raw.get("subnet_groups") or []:
        subnet_type = group["type"]
        if subnet_type not in SUBNET_TYPES:
            raise ValueError(
                f"Subnet group '{group['name']}' has type '{subnet_type}'; "
                f"expected one of {', '.join(SUBNET_TYPES)}."
            )
        groups.append(
            SubnetGroupSpec(
                name=group["name"],
                type=subnet_type,
                cidrs=cidrs[group["name"]],
                map_public_ip_on_launch=bool(group.get("map_public_ip_on_launch", True)),
            )
        )
    return tuple(groups)


def _validate_nat(
    network_name: str, groups: Sequence[SubnetGroupSpec], nat_gateways: int
) -> None:
    types = {group.type for group in groups}

    if "private" in types and nat_gateways < 1:
        raise ValueError(
            f"Network '{network_name}': 'private' subnet groups need nat_gateways >= 1. "
            f"Use type 'isolated' for subnets with no outbound internet access."
        )
    if nat_gateways > 0 and "public" not in types:
        raise ValueError(
            f"Network '{network_name}': nat_gateways > 0 needs a 'public' subnet group "
            f"to place the NAT gateways in."
        )
    if nat_gateways > 0 and "private" not in types:
        raise ValueError(
            f"Network '{network_name}': nat_gateways > 0 but no 'private' subnet group "
            f"would route through them. Set nat_gateways: 0."
        )


def _network(raw: Mapping[str, Any], region: str) -> NetworkSpec:
    azs = _availability_zones(raw, region)
    groups = _subnet_groups(raw, _resolve_cidrs(raw, azs))
    nat_gateways = int(raw.get("nat_gateways", 0))
    _validate_nat(raw["name"], groups, nat_gateways)

    return NetworkSpec(
        name=raw["name"],
        cidr_block=raw["cidr_block"],
        availability_zones=azs,
        nat_gateways=nat_gateways,
        subnet_groups=groups,
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
    region = raw["region"]

    common = CommonProps(
        account_name=raw["common"]["account_name"],
        region_prefix=raw["common"]["region_prefix"],
        tags=_common_tags(raw["common"].get("tags", {})),
        qualifier=str(raw["common"].get("qualifier", "") or ""),
    )

    return EnvironmentConfig(
        name=env_name,
        account_id=str(raw["account_id"]),
        region=region,
        deploy_order=int(raw.get("deploy_order", 100)),
        common=common,
        networks=tuple(_network(network, region) for network in raw.get("networks", [])),
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
