"""Subnet constructs — equivalent of `terraform/modules/subnets` + `modules/Routes`.

`ec2.PublicSubnet` / `ec2.PrivateSubnet` also create the route table and its
association, so the Terraform `Routes` module is folded in here.
"""

from __future__ import annotations

from typing import Sequence

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.config import CommonProps, SubnetSpec
from cognitech_cdk.common.naming import ordinal


def _tag_names(subnet: ec2.Subnet, name: str) -> None:
    """Tag the subnet, then override the route table it owns with its own Name."""
    cdk.Tags.of(subnet).add("Name", name)
    # `RouteTable` is the child id `ec2.Subnet` gives its CfnRouteTable.
    cdk.Tags.of(subnet.node.find_child("RouteTable")).add("Name", f"{name}-rtb")


class PublicSubnets(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: CommonProps,
        vpc_id: str,
        vpc_name: str,
        group_name: str,
        subnets: Sequence[SubnetSpec],
        internet_gateway_id: str,
        internet_gateway_attachment: cdk.CfnResource,
    ) -> None:
        super().__init__(scope, construct_id)

        self.subnets: list[ec2.PublicSubnet] = []

        for index, spec in enumerate(subnets):
            subnet = ec2.PublicSubnet(
                self,
                ordinal(index).capitalize(),
                vpc_id=vpc_id,
                availability_zone=spec.availability_zone,
                cidr_block=spec.cidr_block,
                map_public_ip_on_launch=False,
            )
            subnet.add_default_internet_route(internet_gateway_id, internet_gateway_attachment)
            _tag_names(
                subnet,
                common.resource_name(vpc_name, group_name, "public", ordinal(index)),
            )
            self.subnets.append(subnet)

    @property
    def subnet_ids(self) -> list[str]:
        return [subnet.subnet_id for subnet in self.subnets]


class PrivateSubnets(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: CommonProps,
        vpc_id: str,
        vpc_name: str,
        group_name: str,
        subnets: Sequence[SubnetSpec],
        nat_gateway_ids: Sequence[str] = (),
    ) -> None:
        super().__init__(scope, construct_id)

        self.subnets: list[ec2.PrivateSubnet] = []

        for index, spec in enumerate(subnets):
            subnet = ec2.PrivateSubnet(
                self,
                ordinal(index).capitalize(),
                vpc_id=vpc_id,
                availability_zone=spec.availability_zone,
                cidr_block=spec.cidr_block,
            )
            if nat_gateway_ids:
                # Spread subnets across the available NAT gateways.
                subnet.add_default_nat_route(nat_gateway_ids[index % len(nat_gateway_ids)])
            _tag_names(
                subnet,
                common.resource_name(vpc_name, group_name, "private", ordinal(index)),
            )
            self.subnets.append(subnet)

    @property
    def subnet_ids(self) -> list[str]:
        return [subnet.subnet_id for subnet in self.subnets]
