"""Shared behavior for config-driven deployment stacks."""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from cognitech_cdk.common.environment import Environment


class EnvironmentStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, environment: Environment, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = environment
        for key, value in environment.common.tags.items():
            cdk.Tags.of(self).add(key, value)

    def resource_name(self, *parts: str) -> str:
        return self.config.common.name(self.config.network.name, *parts)

    def output(
        self,
        logical_id: str,
        value: str,
        export_suffix: str,
        *,
        description: str | None = None,
    ) -> cdk.CfnOutput:
        return cdk.CfnOutput(
            self,
            logical_id,
            value=value,
            description=description,
            export_name=f"{self.stack_name}-{export_suffix}",
        )
