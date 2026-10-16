"""IAM roles and instance profiles, independent of the network."""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.iam import Iam


class IamStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, environment: Environment, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        for key, value in environment.common.tags.items():
            cdk.Tags.of(self).add(key, value)

        self.iam = Iam(
            self,
            "Iam",
            common=environment.common,
            env_name=environment.name,
            props=environment.iam,
        )
        for name, role in self.iam.roles.items():
            self._output(f"{name}RoleArn", role.role_arn, f"{name}-role-arn")
            self._output(f"{name}RoleName", role.role_name, f"{name}-role-name")
        for name, profile in self.iam.instance_profiles.items():
            self._output(f"{profile.node.id}Arn", profile.attr_arn, f"{name}-profile-arn")
            self._output(f"{profile.node.id}Name", profile.ref, f"{name}-profile-name")

    def _output(self, logical_id: str, value: str, suffix: str) -> None:
        cdk.CfnOutput(
            self, logical_id, value=value, export_name=f"{self.stack_name}-{suffix}"
        )
