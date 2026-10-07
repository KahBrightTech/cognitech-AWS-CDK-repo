"""VPC with its subnets, route tables, internet gateway and NAT gateways.

Subnets are created explicitly rather than through `ec2.Vpc`'s `subnet_configuration`
so that every CIDR and availability zone stays under the config's control.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.config import CommonProps, NetworkSpec, SubnetGroupSpec
from cognitech_cdk.common.naming import ordinal


class Network(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: CommonProps,
        spec: NetworkSpec,
    ) -> None:
        super().__init__(scope, construct_id)

        self.spec = spec
        self._common = common
        self._groups: dict[str, list[ec2.Subnet]] = {}

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=common.resource_name(spec.name, "vpc"),
            ip_addresses=ec2.IpAddresses.cidr(spec.cidr_block),
            availability_zones=list(spec.availability_zones),
            enable_dns_hostnames=True,
            enable_dns_support=True,
            create_internet_gateway=False,
            nat_gateways=0,
            subnet_configuration=[],
        )

        self.internet_gateway = self._add_internet_gateway()

        for group in spec.subnet_groups:
            if group.type == "public":
                self._add_group(group)

        self.nat_gateway_ids = self._add_nat_gateways()

        for group in spec.subnet_groups:
            if group.type != "public":
                self._add_group(group)

    def _add_internet_gateway(self) -> ec2.CfnInternetGateway | None:
        if not any(group.type == "public" for group in self.spec.subnet_groups):
            return None

        gateway = ec2.CfnInternetGateway(
            self,
            "InternetGateway",
            tags=[
                cdk.CfnTag(
                    key="Name", value=self._common.resource_name(self.spec.name, "igw")
                )
            ],
        )
        self._gateway_attachment = ec2.CfnVPCGatewayAttachment(
            self,
            "InternetGatewayAttachment",
            vpc_id=self.vpc.vpc_id,
            internet_gateway_id=gateway.ref,
        )
        return gateway

    def _add_group(self, group: SubnetGroupSpec) -> None:
        subnets: list[ec2.Subnet] = []

        for index, (zone, cidr) in enumerate(zip(self.spec.availability_zones, group.cidrs)):
            scope_id = f"{group.name.capitalize()}{ordinal(index).capitalize()}"

            if group.type == "public":
                subnet: ec2.Subnet = ec2.PublicSubnet(
                    self,
                    scope_id,
                    vpc_id=self.vpc.vpc_id,
                    availability_zone=zone,
                    cidr_block=cidr,
                    map_public_ip_on_launch=group.map_public_ip_on_launch,
                )
                subnet.add_default_internet_route(
                    self.internet_gateway.ref, self._gateway_attachment
                )
            else:
                subnet = ec2.PrivateSubnet(
                    self,
                    scope_id,
                    vpc_id=self.vpc.vpc_id,
                    availability_zone=zone,
                    cidr_block=cidr,
                )
                if group.type == "private":
                    # Spread the subnets across however many NAT gateways exist.
                    subnet.add_default_nat_route(
                        self.nat_gateway_ids[index % len(self.nat_gateway_ids)]
                    )

            name = self._common.resource_name(
                self.spec.name, group.name, group.type, ordinal(index)
            )
            cdk.Tags.of(subnet).add("Name", name)
            # `RouteTable` is the child id `ec2.Subnet` gives its CfnRouteTable.
            cdk.Tags.of(subnet.node.find_child("RouteTable")).add("Name", f"{name}-rtb")

            subnets.append(subnet)

        self._groups[group.name] = subnets

    def _add_nat_gateways(self) -> list[str]:
        if self.spec.nat_gateways < 1:
            return []

        public = [
            subnet
            for group in self.spec.subnet_groups
            if group.type == "public"
            for subnet in self._groups[group.name]
        ]

        gateway_ids = []
        for index in range(min(self.spec.nat_gateways, len(public))):
            suffix = ordinal(index).capitalize()
            name = self._common.resource_name(self.spec.name, ordinal(index))

            eip = ec2.CfnEIP(
                self,
                f"NatEip{suffix}",
                domain="vpc",
                tags=[cdk.CfnTag(key="Name", value=f"{name}-nat-eip")],
            )
            gateway = ec2.CfnNatGateway(
                self,
                f"NatGateway{suffix}",
                subnet_id=public[index].subnet_id,
                allocation_id=eip.attr_allocation_id,
                tags=[cdk.CfnTag(key="Name", value=f"{name}-natgw")],
            )
            gateway_ids.append(gateway.ref)

        return gateway_ids

    def subnets(self, group_name: str) -> list[ec2.Subnet]:
        return list(self._groups[group_name])

    def subnet_ids(self, group_name: str) -> list[str]:
        return [subnet.subnet_id for subnet in self._groups[group_name]]

    def selection(self, group_name: str) -> ec2.SubnetSelection:
        """Pass to any L2 that takes `vpc_subnets=`, e.g. an ALB or an RDS instance."""
        return ec2.SubnetSelection(subnets=self.subnets(group_name))
