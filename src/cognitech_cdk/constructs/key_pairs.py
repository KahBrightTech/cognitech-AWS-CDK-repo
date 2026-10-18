"""EC2 key pairs (Terraform `EC2-key-pair` module).

Terraform generates the key with `tls_private_key`, so the private key sits in
state and in the secret version. Here EC2 generates the key: CloudFormation
stores the private key as the SecureString `/ec2/keypair/<key-pair-id>`, so the
template never contains it. With `create_secret: true` a small custom resource
copies that parameter into Secrets Manager inside AWS; it never logs the value
and returns no response data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.constructs.secrets import apply_resource_policy, removal_policy, resource_policy

KEY_TYPES = {"rsa": ec2.KeyPairType.RSA, "ed25519": ec2.KeyPairType.ED25519}
KEY_FORMATS = {"pem": ec2.KeyPairFormat.PEM, "ppk": ec2.KeyPairFormat.PPK}

_UUID = "5d4f8b0e-7f39-4b8e-9a51-2f6c0a3e1b72"
_CODE = r'''
import json
import urllib.request

import boto3


def _send(event, context, status, reason=None):
    body = json.dumps({
        "Status": status,
        "Reason": reason or "OK",
        "PhysicalResourceId": event["ResourceProperties"]["SecretArn"],
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": True,
        "Data": {},
    }).encode()
    request = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    urllib.request.urlopen(request, timeout=10).read()


def handler(event, context):
    # Never print the event's parameter value or exception text: only codes.
    try:
        if event["RequestType"] != "Delete":
            props = event["ResourceProperties"]
            parameter = boto3.client("ssm").get_parameter(
                Name=props["ParameterName"], WithDecryption=True
            )["Parameter"]
            boto3.client("secretsmanager").put_secret_value(
                SecretId=props["SecretArn"], SecretString=parameter["Value"]
            )
            parameter = None
        _send(event, context, "SUCCESS")
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)
        _send(event, context, "FAILED", f"private key copy failed: {code}")
'''


@dataclass(frozen=True)
class KeyPairProps:
    name: str
    create_secret: bool = False
    secret_name: str | None = None
    secret_description: str | None = None
    policy: Mapping[str, Any] | None = None
    key_type: str = "rsa"
    format: str = "pem"
    removal_policy: cdk.RemovalPolicy = cdk.RemovalPolicy.RETAIN

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> "KeyPairProps":
        raw = schema.mapping(raw, "key_pairs item", {
            "name", "name_prefix", "secret_name", "secret_description", "policy",
            "create_secret", "key_type", "format", "removal_policy",
        }, required=("name",))
        name = schema.name(raw["name"], "key_pairs item name")
        label = f"key_pairs '{name}'"
        if "name_prefix" in raw:
            raise ValueError(f"{label}: name_prefix is not supported (Terraform ignores it); use name.")
        create_secret = schema.boolean(raw.get("create_secret"), f"{label}.create_secret")
        if not create_secret:
            extra = [key for key in ("secret_name", "secret_description", "policy") if key in raw]
            if extra:
                raise ValueError(f"{label}: {', '.join(extra)} require create_secret: true.")
        key_type = raw.get("key_type", "rsa")
        if key_type not in KEY_TYPES:
            raise ValueError(f"{label}.key_type must be one of: {', '.join(KEY_TYPES)}.")
        key_format = raw.get("format", "pem")
        if key_format not in KEY_FORMATS:
            raise ValueError(f"{label}.format must be one of: {', '.join(KEY_FORMATS)}.")
        secret_name = raw.get("secret_name")
        return cls(
            name=name,
            create_secret=create_secret,
            secret_name=schema.name(secret_name, f"{label}.secret_name") if secret_name is not None else None,
            secret_description=schema.string(raw.get("secret_description"), f"{label}.secret_description"),
            policy=resource_policy(raw.get("policy"), f"{label}.policy", base_dir=base_dir),
            key_type=key_type,
            format=key_format,
            removal_policy=removal_policy(raw.get("removal_policy"), f"{label}.removal_policy"),
        )


@dataclass(frozen=True)
class KeyPairsProps:
    key_pairs: tuple[KeyPairProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.key_pairs)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> "KeyPairsProps":
        return cls(key_pairs=schema.items(
            raw, "key_pairs", lambda item: KeyPairProps.from_dict(item, base_dir=base_dir)
        ))


class KeyPairs(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        props: KeyPairsProps,
        naming: Callable[..., str],
    ) -> None:
        super().__init__(scope, construct_id)
        self.key_pairs: dict[str, ec2.KeyPair] = {}
        self.secrets: dict[str, secretsmanager.Secret] = {}
        self.private_key_parameter_names: dict[str, str] = {}
        self._function: lambda_.SingletonFunction | None = None
        stack = cdk.Stack.of(self)
        for item in props.key_pairs:
            full_name = naming(item.name, "key")
            key_pair = ec2.KeyPair(
                self,
                item.name,
                key_pair_name=full_name,
                type=KEY_TYPES[item.key_type],
                format=KEY_FORMATS[item.format],
            )
            key_pair.apply_removal_policy(item.removal_policy)
            cdk.Tags.of(key_pair).add("Name", full_name)
            self.key_pairs[item.name] = key_pair
            parameter_name = f"/ec2/keypair/{key_pair.key_pair_id}"
            self.private_key_parameter_names[item.name] = parameter_name

            if not item.create_secret:
                continue
            secret_name = naming(item.secret_name or f"{item.name}-private-key")
            secret = secretsmanager.Secret(
                self,
                f"{item.name}Secret",
                secret_name=secret_name,
                description=item.secret_description or f"Private key for EC2 key pair {full_name}",
                removal_policy=item.removal_policy,
            )
            apply_resource_policy(secret, item.policy)
            cdk.Tags.of(secret).add("Name", secret_name)
            self.secrets[item.name] = secret

            function = self._copy_function()
            function.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[stack.format_arn(
                    service="ssm", resource="parameter", resource_name=f"ec2/keypair/{key_pair.key_pair_id}"
                )],
            ))
            function.add_to_role_policy(iam.PolicyStatement(
                actions=["secretsmanager:PutSecretValue"], resources=[secret.secret_arn]
            ))
            cdk.CustomResource(
                self,
                f"{item.name}PrivateKeyCopy",
                service_token=function.function_arn,
                resource_type="Custom::KeyPairPrivateKeyToSecret",
                properties={"ParameterName": parameter_name, "SecretArn": secret.secret_arn},
            )

    def _copy_function(self) -> lambda_.SingletonFunction:
        if self._function is not None:
            return self._function
        stack = cdk.Stack.of(self)
        self._function = lambda_.SingletonFunction(
            self,
            "PrivateKeyCopyFunction",
            uuid=_UUID,
            lambda_purpose="KeyPairPrivateKeyCopy",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline(_CODE),
            timeout=cdk.Duration.minutes(1),
            description="Copies EC2-generated key pair private keys into Secrets Manager",
        )
        # /ec2/keypair/* parameters use the AWS managed aws/ssm key.
        self._function.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt"],
            resources=["*"],
            conditions={"StringEquals": {"kms:ViaService": f"ssm.{stack.region}.amazonaws.com"}},
        ))
        return self._function
