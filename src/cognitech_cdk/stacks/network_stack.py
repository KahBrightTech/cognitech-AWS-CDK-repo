"""Network stack — the CDK equivalent of a Terraform root module.

It calls constructs the same way a root module calls `module "vpc" { ... }`.
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from cognitech_cdk.common.config import CommonProps, NetworkSpec
from cognitech_cdk.constructs.nat_gateway import NatGateways
from cognitech_cdk.constructs.subnets import PrivateSubnets, PublicSubnets
from cognitech_cdk.constructs.vpc import Vpc


class NetworkStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: CommonProps,
        network: NetworkSpec,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        for key, value in common.tags.items():
            cdk.Tags.of(self).add(key, value)

        self.network = Vpc(
            self,
            "Network",
            common=common,
            name=network.name,
            cidr_block=network.cidr_block,
        )

        self.public_subnets: dict[str, PublicSubnets] = {}
        for group in network.public_subnet_groups:
            self.public_subnets[group.name] = PublicSubnets(
                self,
                f"Public{group.name.capitalize()}",
                common=common,
                vpc_id=self.network.vpc_id,
                vpc_name=network.name,
                group_name=group.name,
                subnets=group.subnets,
                internet_gateway_id=self.network.internet_gateway_id,
                internet_gateway_attachment=self.network.internet_gateway_attachment,
            )

        all_public = [s for group in self.public_subnets.values() for s in group.subnets]

        self.nat_gateways = NatGateways(
            self,
            "NatGateways",
            common=common,
            vpc_name=network.name,
            public_subnets=all_public,
            count=network.nat_gateways,
        )

        self.private_subnets: dict[str, PrivateSubnets] = {}
        for group in network.private_subnet_groups:
            self.private_subnets[group.name] = PrivateSubnets(
                self,
                f"Private{group.name.capitalize()}",
                common=common,
                vpc_id=self.network.vpc_id,
                vpc_name=network.name,
                group_name=group.name,
                subnets=group.subnets,
                nat_gateway_ids=self.nat_gateways.nat_gateway_ids,
            )

        cdk.CfnOutput(
            self,
            "VpcId",
            value=self.network.vpc_id,
            export_name=f"{construct_id}-vpc-id",
        )
        for name, group in self.private_subnets.items():
            cdk.CfnOutput(
                self,
                f"PrivateSubnetIds{name.capitalize()}",
                value=cdk.Fn.join(",", group.subnet_ids),
                export_name=f"{construct_id}-private-{name}-subnet-ids",
            )
        for name, group in self.public_subnets.items():
            cdk.CfnOutput(
                self,
                f"PublicSubnetIds{name.capitalize()}",
                value=cdk.Fn.join(",", group.subnet_ids),
                export_name=f"{construct_id}-public-{name}-subnet-ids",
            )
