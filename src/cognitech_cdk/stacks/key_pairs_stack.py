"""EC2 key pairs, with the private key kept inside AWS (SSM, optionally Secrets Manager)."""

from __future__ import annotations

import re

from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.key_pairs import KeyPairs, KeyPairsProps
from cognitech_cdk.stacks.base import EnvironmentStack


def _logical(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name) if part)


class KeyPairsStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: KeyPairsProps,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        built = KeyPairs(self, "KeyPairs", props=props, naming=self.resource_name)
        self.key_pairs: dict[str, ec2.IKeyPair] = dict(built.key_pairs)
        self.secrets: dict[str, secretsmanager.ISecret] = dict(built.secrets)
        self.private_key_parameter_names: dict[str, str] = dict(built.private_key_parameter_names)

        for name, key_pair in built.key_pairs.items():
            logical = _logical(name)
            self.output(f"{logical}KeyPairName", key_pair.key_pair_name, f"{name}-key-pair-name")
            self.output(f"{logical}KeyPairId", key_pair.key_pair_id, f"{name}-key-pair-id")
            self.output(
                f"{logical}PrivateKeyParameterName",
                self.private_key_parameter_names[name],
                f"{name}-private-key-parameter-name",
                description="SecureString parameter holding the EC2-generated private key",
            )
        for name, secret in built.secrets.items():
            logical = _logical(name)
            self.output(f"{logical}PrivateKeySecretArn", secret.secret_arn, f"{name}-private-key-secret-arn")
            self.output(f"{logical}PrivateKeySecretName", secret.secret_name, f"{name}-private-key-secret-name")
