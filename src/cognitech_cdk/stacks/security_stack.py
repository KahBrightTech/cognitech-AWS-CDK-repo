"""Security baseline: the security groups every other stack attaches to.

Separate from the network stack so a rule change does not touch the VPC, and
separate from the stacks that use the groups so a cluster can be replaced
without dropping the rules pointing at it.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.security import SecurityGroups


class SecurityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        vpc: ec2.IVpc,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        for key, value in environment.common.tags.items():
            cdk.Tags.of(self).add(key, value)

        self.security_groups = SecurityGroups(
            self,
            "SecurityGroups",
            common=environment.common,
            props=environment.security_groups,
            vpc=vpc,
        )

        # One export per group, so a later stack can import an id without
        # having to be deployed from the same app.
        for props in environment.security_groups:
            group = self.security_groups[props.name]
            cdk.CfnOutput(
                self,
                f"{group.node.id}SecurityGroupId",
                value=group.security_group_id,
                description=props.description,
                export_name=f"{self.stack_name}-{props.name}-sg-id",
            )
