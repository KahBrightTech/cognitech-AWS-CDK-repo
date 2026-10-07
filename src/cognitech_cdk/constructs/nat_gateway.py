"""NAT gateway construct — equivalent of `terraform/modules/natgateway`."""

from __future__ import annotations

from typing import Sequence

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.config import CommonProps
from cognitech_cdk.common.naming import ordinal


class NatGateways(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: CommonProps,
        vpc_name: str,
        public_subnets: Sequence[ec2.PublicSubnet],
        count: int,
    ) -> None:
        super().__init__(scope, construct_id)

        self.nat_gateway_ids: list[str] = []

        for index in range(min(count, len(public_subnets))):
            suffix = ordinal(index)
            eip = ec2.CfnEIP(
                self,
                f"Eip{suffix.capitalize()}",
                domain="vpc",
                tags=[cdk.CfnTag(key="Name", value=common.resource_name(vpc_name, suffix, "nat-eip"))],
            )
            nat_gateway = ec2.CfnNatGateway(
                self,
                f"NatGateway{suffix.capitalize()}",
                subnet_id=public_subnets[index].subnet_id,
                allocation_id=eip.attr_allocation_id,
                tags=[cdk.CfnTag(key="Name", value=common.resource_name(vpc_name, suffix, "natgw"))],
            )
            self.nat_gateway_ids.append(nat_gateway.ref)
