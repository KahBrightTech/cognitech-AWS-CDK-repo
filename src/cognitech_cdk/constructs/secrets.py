"""Secrets Manager secrets (Terraform `Secrets-manager` module).

Secret values never come from YAML. Terraform's `value` map is replaced by a
`generate` block so Secrets Manager creates the value at deploy time; real
credentials are set out of band with `PutSecretValue`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common import documents, schema

REMOVAL_POLICIES = {
    "retain": cdk.RemovalPolicy.RETAIN,
    "destroy": cdk.RemovalPolicy.DESTROY,
}
POLICY_PLACEHOLDERS = ("account", "region", "partition")
# Condition keys that pin a wildcard principal to a known account, org or network.
RESTRICTIVE_CONDITION_KEYS = {
    "aws:principalaccount", "aws:principalorgid", "aws:principalorgpaths",
    "aws:principalarn", "aws:sourceaccount", "aws:sourcearn", "aws:sourceorgid",
    "aws:sourcevpc", "aws:sourcevpce", "aws:resourceaccount",
}
SENSITIVE_KEY = re.compile(r"pass|secret|token|private|credential|api[_-]?key", re.I)
SECRET_ACTIONS = ("secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue")


def removal_policy(value: Any, label: str) -> cdk.RemovalPolicy:
    if value is None:
        return cdk.RemovalPolicy.RETAIN
    if value not in REMOVAL_POLICIES:
        raise ValueError(f"{label} must be one of: {', '.join(REMOVAL_POLICIES)}.")
    return REMOVAL_POLICIES[value]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _is_public_principal(principal: Any) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, Mapping):
        return any("*" in _as_list(value) for value in principal.values())
    return False


def resource_policy(raw: Any, label: str, *, base_dir: Path | None) -> dict[str, Any] | None:
    """Load a secret resource policy and refuse public grants.

    `[[account]]`, `[[region]]` and `[[partition]]` are replaced at synth time
    with CloudFormation pseudo parameters.
    """
    if raw is None:
        return None
    document = documents.json_compatible(
        documents.load_document(raw, label, base_dir=base_dir), label
    )
    document = schema.mapping(document, label, {"Version", "Id", "Statement"}, required=("Statement",))
    unknown = sorted(documents.placeholders(document) - set(POLICY_PLACEHOLDERS))
    if unknown:
        raise ValueError(f"{label}: unknown placeholders: {', '.join(unknown)}.")
    statements = document["Statement"]
    statements = [statements] if isinstance(statements, Mapping) else statements
    if not isinstance(statements, list) or not statements:
        raise ValueError(f"{label}: Statement must be a statement or a nonempty list.")
    for index, statement in enumerate(statements, start=1):
        where = f"{label}, statement {index}"
        statement = schema.mapping(statement, where, {
            "Sid", "Effect", "Action", "NotAction", "Resource", "NotResource",
            "Principal", "NotPrincipal", "Condition",
        })
        if statement.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f"{where}: Effect must be Allow or Deny.")
        if ("Action" in statement) == ("NotAction" in statement):
            raise ValueError(f"{where}: provide exactly one of Action or NotAction.")
        if "Principal" not in statement:
            raise ValueError(f"{where}: resource policies need a Principal.")
        if statement["Effect"] == "Allow":
            if "NotPrincipal" in statement or "NotAction" in statement:
                raise ValueError(f"{where}: Allow statements must not use NotPrincipal or NotAction.")
            if _is_public_principal(statement["Principal"]):
                condition = statement.get("Condition") or {}
                keys = {
                    str(key).lower()
                    for operator in condition.values() if isinstance(operator, Mapping)
                    for key in operator
                }
                if not keys & RESTRICTIVE_CONDITION_KEYS:
                    raise ValueError(
                        f"{where}: Principal '*' is public; restrict it with a Condition on "
                        f"one of {', '.join(sorted(RESTRICTIVE_CONDITION_KEYS))} "
                        "or name the account/role principals instead."
                    )
    return {**document, "Statement": statements}


def apply_resource_policy(
    secret: secretsmanager.Secret, policy: Mapping[str, Any] | None
) -> None:
    """Attach the configured policy, or account-root-only access by default."""
    if policy is None:
        secret.add_to_resource_policy(iam.PolicyStatement(
            sid="AccountRootOnly",
            principals=[iam.AccountRootPrincipal()],
            actions=list(SECRET_ACTIONS),
            resources=["*"],
        ))
        return
    values = {"account": cdk.Aws.ACCOUNT_ID, "region": cdk.Aws.REGION, "partition": cdk.Aws.PARTITION}
    rendered = documents.render(policy, values, "secret policy")
    for statement in rendered["Statement"]:
        secret.add_to_resource_policy(iam.PolicyStatement.from_json(statement))


@dataclass(frozen=True)
class SecretGeneratorProps:
    """Deploy-time generated value. `template` holds non-secret fields only."""

    template: Mapping[str, str] = field(default_factory=dict)
    key: str | None = None
    length: int | None = None
    exclude_characters: str | None = None
    exclude_punctuation: bool = False
    include_space: bool = False
    require_each_included_type: bool = True

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "SecretGeneratorProps":
        if raw is None:
            return cls()
        raw = schema.mapping(raw, label, {
            "template", "key", "length", "exclude_characters",
            "exclude_punctuation", "include_space", "require_each_included_type",
        })
        template = schema.string_map(raw.get("template"), f"{label}.template")
        sensitive = sorted(key for key in template if SENSITIVE_KEY.search(key))
        if sensitive:
            raise ValueError(
                f"{label}.template: '{', '.join(sensitive)}' looks secret; template values are "
                "stored in the CloudFormation template. Use generate.key for the secret field."
            )
        key = schema.string(raw.get("key"), f"{label}.key")
        if bool(template) != (key is not None):
            raise ValueError(f"{label}: template and key must be set together.")
        if key is not None and key in template:
            raise ValueError(f"{label}: key '{key}' must not also appear in template.")
        return cls(
            template=template,
            key=key,
            length=schema.integer(raw.get("length"), f"{label}.length", minimum=8, maximum=4096),
            exclude_characters=schema.string(raw.get("exclude_characters"), f"{label}.exclude_characters"),
            exclude_punctuation=schema.boolean(raw.get("exclude_punctuation"), f"{label}.exclude_punctuation"),
            include_space=schema.boolean(raw.get("include_space"), f"{label}.include_space"),
            require_each_included_type=schema.boolean(
                raw.get("require_each_included_type"), f"{label}.require_each_included_type", default=True
            ),
        )

    def generator(self) -> secretsmanager.SecretStringGenerator:
        return secretsmanager.SecretStringGenerator(
            secret_string_template=json.dumps(dict(self.template), sort_keys=True) if self.key else None,
            generate_string_key=self.key,
            password_length=self.length,
            exclude_characters=self.exclude_characters,
            exclude_punctuation=self.exclude_punctuation,
            include_space=self.include_space,
            require_each_included_type=self.require_each_included_type,
        )


@dataclass(frozen=True)
class SecretProps:
    key: str
    description: str
    name: str | None = None
    name_prefix: str | None = None
    recovery_window_in_days: int | None = None
    policy: Mapping[str, Any] | None = None
    generate: SecretGeneratorProps = SecretGeneratorProps()
    removal_policy: cdk.RemovalPolicy = cdk.RemovalPolicy.RETAIN

    @property
    def base_name(self) -> str:
        return self.name or self.name_prefix  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> "SecretProps":
        raw = schema.mapping(raw, "secrets item", {
            "key", "name", "name_prefix", "description", "recovery_window_in_days",
            "policy", "value", "generate", "removal_policy",
        }, required=("key",))
        key = schema.name(raw["key"], "secrets item key")
        label = f"secrets '{key}'"
        if "value" in raw:
            raise ValueError(
                f"{label}: literal 'value' is not supported because it would be stored in the "
                "template and repository. Use 'generate' or set the value out of band."
            )
        name = raw.get("name")
        name_prefix = raw.get("name_prefix")
        if (name is None) == (name_prefix is None):
            raise ValueError(f"{label}: set exactly one of name or name_prefix.")
        return cls(
            key=key,
            description=schema.required_string(raw.get("description"), f"{label}.description"),
            name=schema.name(name, f"{label}.name") if name is not None else None,
            name_prefix=schema.name(name_prefix, f"{label}.name_prefix") if name_prefix is not None else None,
            recovery_window_in_days=_recovery_window(raw.get("recovery_window_in_days"), label),
            policy=resource_policy(raw.get("policy"), f"{label}.policy", base_dir=base_dir),
            generate=SecretGeneratorProps.from_dict(raw.get("generate"), f"{label}.generate"),
            removal_policy=removal_policy(raw.get("removal_policy"), f"{label}.removal_policy"),
        )


def _recovery_window(value: Any, label: str) -> int | None:
    days = schema.integer(value, f"{label}.recovery_window_in_days", minimum=0, maximum=30)
    if days is not None and 0 < days < 7:
        raise ValueError(f"{label}.recovery_window_in_days must be 0 or between 7 and 30.")
    return days


@dataclass(frozen=True)
class SecretsProps:
    secrets: tuple[SecretProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.secrets)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> "SecretsProps":
        return cls(secrets=schema.items(
            raw, "secrets", lambda item: SecretProps.from_dict(item, base_dir=base_dir),
            key=lambda item: item.key,
        ))


class Secrets(Construct):
    """One `AWS::SecretsManager::Secret` per configured item, keyed by `key`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        props: SecretsProps,
        naming: Callable[..., str],
    ) -> None:
        super().__init__(scope, construct_id)
        self.secrets: dict[str, secretsmanager.Secret] = {}
        for item in props.secrets:
            full_name = naming(item.base_name)
            secret = secretsmanager.Secret(
                self,
                item.key,
                secret_name=full_name,
                description=item.description,
                generate_secret_string=item.generate.generator(),
                removal_policy=item.removal_policy,
            )
            if item.policy is not None:
                apply_resource_policy(secret, item.policy)
            cdk.Tags.of(secret).add("Name", full_name)
            self.secrets[item.key] = secret
