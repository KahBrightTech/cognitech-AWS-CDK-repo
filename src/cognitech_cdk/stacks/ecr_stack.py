"""ECR repositories, independent of the network."""

from __future__ import annotations

from aws_cdk import aws_ecr as ecr
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.ecr import EcrProps, EcrRepositories, logical_id
from cognitech_cdk.stacks.base import EnvironmentStack


class EcrStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: EcrProps,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        self.ecr = EcrRepositories(
            self,
            "Ecr",
            props=props,
            name=self.resource_name,
            account_name_abr=environment.common.account_name_abr,
        )
        self.repositories: dict[str, ecr.IRepository] = self.ecr.repositories

        for name, resource in self.ecr.resources.items():
            base = logical_id(name)
            self.output(f"{base}RepositoryArn", resource.attr_arn, f"{name}-repository-arn")
            self.output(
                f"{base}RepositoryUri", resource.attr_repository_uri, f"{name}-repository-uri"
            )
            self.output(f"{base}RepositoryName", resource.ref, f"{name}-repository-name")
