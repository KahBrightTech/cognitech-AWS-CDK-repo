"""VPC with an internet gateway, public subnets and optional private subnets.

Set `private_subnets: true` to also get private subnets, NAT gateways and their
Elastic IPs. Subnet CIDRs can be listed explicitly, or left out and carved from
the VPC range.

Subnets are created one at a time rather than through `ec2.Vpc`'s
`subnet_configuration`, because that property only accepts a prefix length and
this module has to honour exact ranges from the config.

Resource names follow the following naming convention:

    {account_name}-{region_prefix}-{vpc_name}-vpc
    {account_name}-{region_prefix}-{vpc_name}-igw
    {account_name}-{region_prefix}-{vpc_name}-{subnet_name}-{type}-{ordinal}
    {account_name}-{region_prefix}-{vpc_name}-{subnet_name}-ngw-{ordinal}
    {account_name}-{region_prefix}-{vpc_name}-{subnet_name}-eip-{ordinal}
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.config import MAX_AZS, Common, ordinal

DEFAULT_CIDR_MASK = 24


@dataclass(frozen=True)
class NetworkProps:
    """Inputs for the `Network` construct."""

    name: str
    cidr_block: str
    azs: int = 2
    private_subnets: bool = False
    nat_gateways: int | None = None
    # Exact ranges, one per AZ, in order. None carves them from `cidr_block`.
    public_subnet_cidrs: Sequence[str] | None = None
    private_subnet_cidrs: Sequence[str] | None = None
    # Prefix length used when a tier has no explicit CIDRs.
    cidr_mask: int = DEFAULT_CIDR_MASK
    # Names used in the middle of each subnet's resource name.
    public_subnet_name: str = "public"
    private_subnet_name: str = "private"

    def __post_init__(self) -> None:
        if not 1 <= self.azs <= MAX_AZS:
            raise ValueError(
                f"Network '{self.name}': azs must be between 1 and {MAX_AZS}, got {self.azs}."
            )
        if self.nat_gateways is not None and self.nat_gateways < 0:
            raise ValueError(f"Network '{self.name}': nat_gateways cannot be negative.")
        if self.private_subnets and self.nat_gateway_count < 1:
            raise ValueError(
                f"Network '{self.name}': private subnets need at least one NAT gateway. "
                f"Set private_subnets: false for a VPC with no NAT gateways."
            )
        # Fail fast on bad CIDRs rather than at deploy time.
        self.subnet_cidrs()

    @property
    def nat_gateway_count(self) -> int:
        """One NAT gateway per AZ unless told otherwise, and none without private subnets."""
        if not self.private_subnets:
            return 0
        if self.nat_gateways is None:
            return self.azs
        return min(self.nat_gateways, self.azs)

    def subnet_cidrs(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return `(public_cidrs, private_cidrs)`, one entry per AZ.

        Explicit ranges are taken as given and reserved first; any tier without
        them is carved from the lowest free `/{cidr_mask}` blocks in the VPC.
        """
        vpc = ipaddress.ip_network(self.cidr_block)
        taken: list[Any] = []

        explicit = (
            (self.public_subnet_name, self.public_subnet_cidrs),
            (self.private_subnet_name, self.private_subnet_cidrs),
        )
        for tier, cidrs in explicit:
            if cidrs is None:
                continue
            if len(cidrs) != self.azs:
                raise ValueError(
                    f"Network '{self.name}': {tier} lists {len(cidrs)} CIDRs but azs is "
                    f"{self.azs}. Give one CIDR per availability zone, in order."
                )
            for cidr in cidrs:
                block = ipaddress.ip_network(cidr)
                if not block.subnet_of(vpc):
                    raise ValueError(
                        f"Network '{self.name}': subnet CIDR {block} is outside the VPC "
                        f"range {vpc}."
                    )
                for other in taken:
                    if block.overlaps(other):
                        raise ValueError(
                            f"Network '{self.name}': subnet CIDRs {block} and {other} overlap."
                        )
                taken.append(block)

        def resolve(cidrs: Sequence[str] | None, wanted: int) -> tuple[str, ...]:
            if cidrs is not None:
                return tuple(str(ipaddress.ip_network(c)) for c in cidrs)
            carved = []
            for _ in range(wanted):
                block = self._next_free(vpc, taken)
                taken.append(block)
                carved.append(str(block))
            return tuple(carved)

        public = resolve(self.public_subnet_cidrs, self.azs)
        private = resolve(self.private_subnet_cidrs, self.azs if self.private_subnets else 0)
        return public, private

    def _next_free(self, vpc: Any, taken: Sequence[Any]) -> Any:
        for candidate in vpc.subnets(new_prefix=self.cidr_mask):
            if not any(candidate.overlaps(block) for block in taken):
                return candidate
        raise ValueError(
            f"Network '{self.name}': no free /{self.cidr_mask} block left in {vpc}."
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NetworkProps":
        def cidrs(key: str) -> tuple[str, ...] | None:
            value = raw.get(key)
            return tuple(str(c) for c in value) if value else None

        return cls(
            name=str(raw["name"]),
            cidr_block=str(raw["cidr_block"]),
            azs=int(raw.get("azs", 2)),
            private_subnets=bool(raw.get("private_subnets", False)),
            nat_gateways=(
                int(raw["nat_gateways"]) if raw.get("nat_gateways") is not None else None
            ),
            public_subnet_cidrs=cidrs("public_subnet_cidrs"),
            private_subnet_cidrs=cidrs("private_subnet_cidrs"),
            cidr_mask=int(raw.get("cidr_mask", DEFAULT_CIDR_MASK)),
            public_subnet_name=str(raw.get("public_subnet_name", "public")),
            private_subnet_name=str(raw.get("private_subnet_name", "private")),
        )


class Network(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: Common,
        props: NetworkProps,
        region: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.common = common
        self.props = props
        self.availability_zones = [f"{region}{letter}" for letter in "abcd"[: props.azs]]
        public_cidrs, private_cidrs = props.subnet_cidrs()

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=common.name(props.name, "vpc"),
            ip_addresses=ec2.IpAddresses.cidr(props.cidr_block),
            availability_zones=self.availability_zones,
            enable_dns_hostnames=True,
            enable_dns_support=True,
            # Everything below is built explicitly so the CIDRs stay under config control.
            create_internet_gateway=False,
            nat_gateways=0,
            subnet_configuration=[],
        )

        self.internet_gateway, self._igw_attachment = self._add_internet_gateway()
        self.public_subnets = self._add_public_subnets(public_cidrs)
        self.nat_gateway_ids = self._add_nat_gateways()
        self.private_subnets = self._add_private_subnets(private_cidrs)

        self._name_gateways()

    def subnet_selection(self, subnet_type: str) -> ec2.SubnetSelection:
        """Pass to any L2 that takes `vpc_subnets=`, e.g. an ALB or an RDS instance."""
        subnets = self.public_subnets if subnet_type == "public" else self.private_subnets
        return ec2.SubnetSelection(subnets=list(subnets))

    # --- resources --------------------------------------------------------

    def _add_internet_gateway(
        self,
    ) -> tuple[ec2.CfnInternetGateway, ec2.CfnVPCGatewayAttachment]:
        gateway = ec2.CfnInternetGateway(self, "InternetGateway")
        attachment = ec2.CfnVPCGatewayAttachment(
            self,
            "InternetGatewayAttachment",
            vpc_id=self.vpc.vpc_id,
            internet_gateway_id=gateway.ref,
        )
        return gateway, attachment

    def _add_public_subnets(self, cidrs: Sequence[str]) -> list[ec2.PublicSubnet]:
        subnets = []
        for index, (zone, cidr) in enumerate(zip(self.availability_zones, cidrs)):
            subnet = ec2.PublicSubnet(
                self,
                f"PublicSubnet{ordinal(index).capitalize()}",
                vpc_id=self.vpc.vpc_id,
                availability_zone=zone,
                cidr_block=cidr,
                map_public_ip_on_launch=True,
            )
            subnet.add_default_internet_route(self.internet_gateway.ref, self._igw_attachment)
            self._name_subnet(subnet, self.props.public_subnet_name, "public", index)
            subnets.append(subnet)
        return subnets

    def _add_private_subnets(self, cidrs: Sequence[str]) -> list[ec2.PrivateSubnet]:
        subnets = []
        for index, (zone, cidr) in enumerate(zip(self.availability_zones, cidrs)):
            subnet = ec2.PrivateSubnet(
                self,
                f"PrivateSubnet{ordinal(index).capitalize()}",
                vpc_id=self.vpc.vpc_id,
                availability_zone=zone,
                cidr_block=cidr,
            )
            # Spread the subnets across however many NAT gateways exist.
            subnet.add_default_nat_route(self.nat_gateway_ids[index % len(self.nat_gateway_ids)])
            self._name_subnet(subnet, self.props.private_subnet_name, "private", index)
            subnets.append(subnet)
        return subnets

    def _add_nat_gateways(self) -> list[str]:
        gateway_ids = []
        for index in range(min(self.props.nat_gateway_count, len(self.public_subnets))):
            suffix = ordinal(index).capitalize()
            eip = ec2.CfnEIP(self, f"NatEip{suffix}", domain="vpc")
            gateway = ec2.CfnNatGateway(
                self,
                f"NatGateway{suffix}",
                subnet_id=self.public_subnets[index].subnet_id,
                allocation_id=eip.attr_allocation_id,
            )
            gateway_ids.append(gateway.ref)
        return gateway_ids

    # --- naming -----------------------------------------------------------

    @staticmethod
    def _group(subnet_name: str, subnet_type: str) -> str:
        """Avoid "public-public" when a tier is not given its own name."""
        return "" if subnet_name == subnet_type else subnet_name

    def _name_subnet(
        self, subnet: ec2.Subnet, subnet_name: str, subnet_type: str, index: int
    ) -> None:
        group = self._group(subnet_name, subnet_type)
        position = ordinal(index)
        cdk.Tags.of(subnet).add(
            "Name", self.common.name(self.props.name, group, subnet_type, position)
        )
        # `RouteTable` is the child id ec2.Subnet gives its CfnRouteTable.
        cdk.Tags.of(subnet.node.find_child("RouteTable")).add(
            "Name", self.common.name(self.props.name, group, position, subnet_type, "rt")
        )

    def _name_gateways(self) -> None:
        name = self.common.name
        vpc_name = self.props.name

        cdk.Tags.of(self.internet_gateway).add("Name", name(vpc_name, "igw"))

        group = self._group(self.props.public_subnet_name, "public")
        for index in range(len(self.nat_gateway_ids)):
            suffix = ordinal(index).capitalize()
            position = ordinal(index)
            cdk.Tags.of(self.node.find_child(f"NatGateway{suffix}")).add(
                "Name", name(vpc_name, group, "ngw", position)
            )
            cdk.Tags.of(self.node.find_child(f"NatEip{suffix}")).add(
                "Name", name(vpc_name, group, "eip", position)
            )
