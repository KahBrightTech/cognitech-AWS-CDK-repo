"""Network baseline: the VPC and everything inside it."""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.network import Network


class NetworkStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, environment: Environment, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.node.set_context(
            f"availability-zones:account={environment.account_id}:region={environment.region}",
            [f"{environment.region}{letter}" for letter in "abcd"],
        )
        for key, value in environment.common.tags.items():
            cdk.Tags.of(self).add(key, value)

        self.network = Network(
            self,
            "Network",
            common=environment.common,
            props=environment.network,
            region=environment.region,
        )

        self._output("VpcId", self.network.vpc.vpc_id, "vpc-id")
        self._output(
            "PublicSubnetIds",
            cdk.Fn.join(",", [s.subnet_id for s in self.network.public_subnets]),
            "public-subnet-ids",
        )
        if environment.network.private_subnets:
            self._output(
                "PrivateSubnetIds",
                cdk.Fn.join(",", [s.subnet_id for s in self.network.private_subnets]),
                "private-subnet-ids",
            )

    def _output(self, logical_id: str, value: str, export_suffix: str) -> None:
        cdk.CfnOutput(
            self, logical_id, value=value, export_name=f"{self.stack_name}-{export_suffix}"
        )
