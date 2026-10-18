"""Network baseline: the VPC and everything inside it."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.config import ordinal
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
        self._subnet_outputs("public", self.network.public_subnets)
        if environment.network.private_subnets:
            self._subnet_outputs("private", self.network.private_subnets)

    def _subnet_outputs(self, subnet_type: str, subnets: list[ec2.ISubnet]) -> None:
        title = subnet_type.capitalize()
        self._output(
            f"{title}SubnetIds",
            cdk.Fn.join(",", [subnet.subnet_id for subnet in subnets]),
            f"{subnet_type}-subnet-ids",
        )
        for index, subnet in enumerate(subnets):
            position = ordinal(index)
            self._output(
                f"{title}Subnet{position.capitalize()}Id",
                subnet.subnet_id,
                f"{subnet_type}-subnet-{position}-id",
            )

    def _output(self, logical_id: str, value: str, export_suffix: str) -> None:
        cdk.CfnOutput(
            self, logical_id, value=value, export_name=f"{self.stack_name}-{export_suffix}"
        )
