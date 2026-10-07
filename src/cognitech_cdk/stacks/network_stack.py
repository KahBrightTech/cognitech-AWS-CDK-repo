"""Network baseline: one VPC with its subnets and gateways."""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from cognitech_cdk.common.config import CommonProps, NetworkSpec
from cognitech_cdk.constructs.network import Network


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

        self.network = Network(self, "Network", common=common, spec=network)

        cdk.CfnOutput(
            self,
            "VpcId",
            value=self.network.vpc.vpc_id,
            export_name=f"{construct_id}-vpc-id",
        )
        for group in network.subnet_groups:
            cdk.CfnOutput(
                self,
                f"SubnetIds{group.name.capitalize()}",
                value=cdk.Fn.join(",", self.network.subnet_ids(group.name)),
                export_name=f"{construct_id}-{group.name}-subnet-ids",
            )
