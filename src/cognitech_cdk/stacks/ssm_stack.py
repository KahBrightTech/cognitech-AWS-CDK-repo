"""SSM Parameter Store parameters and SSM documents with optional associations."""

from __future__ import annotations

import re
from typing import Mapping

from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.ssm import Ssm, SsmProps
from cognitech_cdk.stacks.base import EnvironmentStack


def _logical(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name) if part)


class SsmStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: SsmProps,
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
        buckets: Mapping[str, s3.IBucket] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        built = Ssm(
            self, "Ssm", props=props, naming=self.resource_name, secrets=secrets, buckets=buckets
        )
        self.parameters: dict[str, ssm.IParameter] = dict(built.parameters)
        self.documents: dict[str, ssm.CfnDocument] = dict(built.documents)
        self.associations: dict[str, ssm.CfnAssociation] = dict(built.associations)

        for name, parameter in built.parameters.items():
            logical = _logical(name)
            self.output(f"{logical}ParameterName", parameter.parameter_name, f"{name}-parameter-name")
            self.output(f"{logical}ParameterArn", parameter.parameter_arn, f"{name}-parameter-arn")
        for name, document in built.documents.items():
            self.output(f"{_logical(name)}DocumentName", document.ref, f"{name}-document-name")
        for name, association in built.associations.items():
            self.output(
                f"{_logical(name)}AssociationId", association.attr_association_id, f"{name}-association-id"
            )
