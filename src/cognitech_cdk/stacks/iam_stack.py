"""IAM roles, instance profiles, customer-managed policies and users.

Independent of the network: nothing here imports VPC or security group values.
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.iam import Iam, IamProps


class IamStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: IamProps | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        for key, value in environment.common.tags.items():
            cdk.Tags.of(self).add(key, value)

        self.iam = Iam(
            self,
            "Iam",
            common=environment.common,
            env_name=environment.name,
            props=environment.iam if props is None else props,
        )
        for name, role in self.iam.roles.items():
            self.output(f"{name}RoleArn", role.role_arn, f"{name}-role-arn")
            self.output(f"{name}RoleName", self.iam.role_names[name], f"{name}-role-name")
        for name, profile in self.iam.instance_profiles.items():
            self.output(f"{profile.node.id}Arn", profile.attr_arn, f"{name}-profile-arn")
            self.output(f"{profile.node.id}Name", profile.ref, f"{name}-profile-name")
        for name, policy in self.iam.managed_policies.items():
            self.output(f"{name}PolicyArn", policy.managed_policy_arn, f"{name}-policy-arn")
            self.output(
                f"{name}PolicyName", self.iam.managed_policy_names[name], f"{name}-policy-name"
            )
        for name, group in self.iam.groups.items():
            self.output(f"{name}GroupArn", group.group_arn, f"{name}-group-arn")
            self.output(f"{name}GroupName", group.group_name, f"{name}-group-name")
        for name, user in self.iam.users.items():
            self.output(f"{name}UserArn", user.user_arn, f"{name}-user-arn")
            self.output(f"{name}UserName", user.user_name, f"{name}-user-name")
        for name, secret in self.iam.user_secrets.items():
            # The secret's ARN only; the access key itself is never an output.
            self.output(
                f"{name}CredentialsSecretArn", secret.secret_arn,
                f"{name}-user-credentials-secret-arn",
            )

    def output(self, logical_id: str, value: str, export_suffix: str) -> cdk.CfnOutput:
        return cdk.CfnOutput(
            self, logical_id, value=value, export_name=f"{self.stack_name}-{export_suffix}"
        )
