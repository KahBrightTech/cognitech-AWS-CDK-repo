"""Config-driven roles, permission/trust policies and EC2 instance profiles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

import aws_cdk as cdk
import yaml
from aws_cdk import aws_iam as iam
from constructs import Construct

from cognitech_cdk.common.config import Common


def _mapping(value: Any, label: str, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping.")
    unknown = set(value) - keys
    if unknown:
        raise ValueError(f"{label}: unknown fields: {', '.join(map(str, unknown))}.")
    return value


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", value
    ):
        raise ValueError(
            f"{label} must start with a letter or digit and use letters, "
            "digits, '-' or '_'."
        )
    return value


def _strings(value: Any, label: str) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or any(
        not isinstance(item, str) or not item.strip() for item in values
    ):
        raise ValueError(f"{label} must be a string or a nonempty list of strings.")
    return values


def _statements(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = document["Statement"]
    return [value] if isinstance(value, Mapping) else value


def _policy_document(
    raw: Any, label: str, *, trust: bool = False, base_dir: Path | None = None
) -> dict[str, Any]:
    if isinstance(raw, str):
        if (
            not raw.strip() or "\0" in raw or "://" in raw
            or Path(raw).is_absolute() or PureWindowsPath(raw).anchor
        ):
            raise ValueError(f"{label}: policy file must be a local relative path.")
        if base_dir is None:
            raise ValueError(f"{label}: base_dir is required to resolve policy file '{raw}'.")
        resolved_base = base_dir.resolve()
        path = (resolved_base / raw).resolve()
        if not path.is_relative_to(resolved_base):
            raise ValueError(
                f"{label}: policy file path must be relative and remain under "
                "the repository root."
            )
        label = f"{label}, file '{path}'"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ValueError(f"{label}: cannot read policy file: {error}") from error
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ValueError(f"{label}: invalid YAML: {error}") from error
    document = _mapping(raw, label, {"Version", "Id", "Statement"})
    if "Version" in document and document["Version"] not in (
        "2012-10-17", "2008-10-17"
    ):
        raise ValueError(f"{label}: Version must be a quoted IAM version string.")
    if "Id" in document and not isinstance(document["Id"], str):
        raise ValueError(f"{label}: Id must be a string.")
    statements = document.get("Statement")
    if not isinstance(statements, (Mapping, list)) or not statements:
        raise ValueError(f"{label}: Statement must be a statement or a nonempty list.")
    for index, raw_statement in enumerate(_statements(document)):
        where = f"{label}, statement {index + 1}"
        statement = _mapping(raw_statement, where, {
            "Sid", "Effect", "Action", "NotAction", "Resource", "NotResource",
            "Principal", "NotPrincipal", "Condition",
        })
        if statement.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f"{where}: Effect must be Allow or Deny.")
        if "Sid" in statement and not isinstance(statement["Sid"], str):
            raise ValueError(f"{where}: Sid must be a string.")
        if ("Action" in statement) == ("NotAction" in statement):
            raise ValueError(f"{where}: provide exactly one of Action or NotAction.")
        action_key = "Action" if "Action" in statement else "NotAction"
        _strings(statement[action_key], f"{where}: {action_key}")
        if trust:
            if "Resource" in statement or "NotResource" in statement:
                raise ValueError(f"{where}: trust policies must not specify Resource.")
            if ("Principal" in statement) == ("NotPrincipal" in statement):
                raise ValueError(
                    f"{where}: provide exactly one of Principal or NotPrincipal."
                )
            principal = statement.get("Principal", statement.get("NotPrincipal"))
            if principal != "*":
                principal = _mapping(
                    principal, f"{where}: Principal",
                    {"AWS", "Service", "Federated", "CanonicalUser"},
                )
                if not principal:
                    raise ValueError(f"{where}: Principal must not be empty.")
                for kind, value in principal.items():
                    _strings(value, f"{where}: Principal.{kind}")
        else:
            if "Principal" in statement or "NotPrincipal" in statement:
                raise ValueError(
                    f"{where}: role permission policies must not specify Principal."
                )
            if ("Resource" in statement) == ("NotResource" in statement):
                raise ValueError(
                    f"{where}: provide exactly one of Resource or NotResource."
                )
            resource_key = "Resource" if "Resource" in statement else "NotResource"
            _strings(statement[resource_key], f"{where}: {resource_key}")
        if "Condition" in statement:
            conditions = statement["Condition"]
            if not isinstance(conditions, Mapping) or not conditions or any(
                not isinstance(operator, str)
                or not isinstance(values, Mapping) or not values
                for operator, values in conditions.items()
            ):
                raise ValueError(
                    f"{where}: Condition must map operators to condition mappings."
                )
    try:
        return json.loads(json.dumps(document, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{label}: policy must contain JSON-compatible values: {error}"
        ) from error


@dataclass(frozen=True)
class IamRoleProps:
    name: str
    assumed_by: str | None = None
    description: str | None = None
    managed_policies: tuple[str, ...] = ()
    managed_policy_arns: tuple[str, ...] = ()
    custom_policies: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    trust_policy: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if (self.assumed_by is None) == (self.trust_policy is None):
            raise ValueError(
                f"IAM role '{self.name}': provide exactly one of assumed_by or trust_policy."
            )

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> IamRoleProps:
        data = _mapping(
            raw, "IAM role", {
                "name", "assumed_by", "description", "managed_policies",
                "managed_policy_arns", "custom_policies", "trust_policy",
            }
        )
        name = _name(data.get("name"), "IAM role name")
        if ("assumed_by" in data) == ("trust_policy" in data):
            raise ValueError(
                f"IAM role '{name}': provide exactly one of assumed_by or trust_policy."
            )
        service = data.get("assumed_by")
        if "assumed_by" in data and (
            not isinstance(service, str) or not re.fullmatch(
                r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.amazonaws\.com(?:\.cn)?", service
            )
        ):
            raise ValueError(
                f"IAM role '{name}': assumed_by must be an AWS service, "
                "e.g. ec2.amazonaws.com."
            )
        description = data.get("description")
        if description is not None and (
            not isinstance(description, str) or not 1 <= len(description) <= 1000
        ):
            raise ValueError(
                f"IAM role '{name}': description must contain 1-1000 characters."
            )
        policies = data.get("managed_policies", [])
        if not isinstance(policies, list) or any(
            not isinstance(policy, str)
            or not re.fullmatch(r"(?:[\w+=,.@-]+/)*[\w+=,.@-]+", policy, flags=re.ASCII)
            for policy in policies
        ):
            raise ValueError(
                f"IAM role '{name}': managed_policies must list "
                "AWS-managed policy names, not ARNs."
            )
        if len(set(policies)) != len(policies):
            raise ValueError(f"IAM role '{name}': duplicate managed policy.")
        arns = data.get("managed_policy_arns", [])
        if not isinstance(arns, list) or any(
            not isinstance(arn, str) or not re.fullmatch(
                r"arn:[a-z0-9-]+:iam::(?:[0-9]{12}|aws):policy/(?:[\w+=,.@-]+/)*[\w+=,.@-]+",
                arn, flags=re.ASCII,
            )
            for arn in arns
        ):
            raise ValueError(
                f"IAM role '{name}': managed_policy_arns must list IAM policy ARNs."
            )
        if len(set(arns)) != len(arns) or any(
            ":iam::aws:policy/" in arn and arn.split(":policy/", 1)[1] in policies
            for arn in arns
        ):
            raise ValueError(f"IAM role '{name}': duplicate managed policy.")
        inline = data.get("custom_policies", {})
        if not isinstance(inline, Mapping):
            raise ValueError(
                f"IAM role '{name}': custom_policies must map names to policy documents."
            )
        custom_policies = {}
        for policy_name, document in inline.items():
            if not isinstance(policy_name, str) or not re.fullmatch(
                r"[\w+=,.@-]{1,128}", policy_name, flags=re.ASCII
            ):
                raise ValueError(
                    f"IAM role '{name}': invalid inline policy name '{policy_name}'."
                )
            custom_policies[policy_name] = _policy_document(
                document, f"IAM role '{name}', custom policy '{policy_name}'",
                base_dir=base_dir,
            )
        trust_policy = (
            _policy_document(
                data["trust_policy"], f"IAM role '{name}', trust_policy",
                trust=True, base_dir=base_dir,
            )
            if "trust_policy" in data else None
        )
        return cls(
            name=name, assumed_by=service, description=description,
            managed_policies=tuple(policies), managed_policy_arns=tuple(arns),
            custom_policies=custom_policies, trust_policy=trust_policy,
        )

    @property
    def trusts_ec2(self) -> bool:
        services = ("ec2.amazonaws.com", "ec2.amazonaws.com.cn")
        if self.assumed_by is not None:
            return self.assumed_by in services
        if self.trust_policy is None:
            return False
        for statement in _statements(self.trust_policy):
            principal = statement.get("Principal")
            if statement["Effect"] != "Allow" or not isinstance(principal, Mapping):
                continue
            if "Service" not in principal:
                continue
            if not any(
                service in _strings(principal["Service"], "Service principal")
                for service in services
            ):
                continue
            actions = statement.get("Action", [])
            if actions and any(
                fnmatchcase("sts:AssumeRole", action)
                for action in _strings(actions, "Trust actions")
            ):
                return True
        return False


@dataclass(frozen=True)
class InstanceProfileProps:
    name: str
    role: str

    @classmethod
    def from_dict(cls, raw: Any) -> InstanceProfileProps:
        data = _mapping(raw, "Instance profile", {"name", "role"})
        return cls(
            _name(data.get("name"), "Instance profile name"),
            _name(data.get("role"), "Instance profile role"),
        )


@dataclass(frozen=True)
class IamProps:
    roles: tuple[IamRoleProps, ...] = ()
    instance_profiles: tuple[InstanceProfileProps, ...] = ()

    def __post_init__(self) -> None:
        for label, entries in (
            ("role", self.roles), ("instance profile", self.instance_profiles)
        ):
            names = [entry.name.casefold() for entry in entries]
            if len(names) != len(set(names)):
                raise ValueError(
                    f"IAM: duplicate {label} name (names are case-insensitive in AWS)."
                )
        roles = {role.name: role for role in self.roles}
        for profile in self.instance_profiles:
            if profile.role not in roles:
                raise ValueError(
                    f"Instance profile '{profile.name}': unknown role '{profile.role}'."
                )
            if not roles[profile.role].trusts_ec2:
                raise ValueError(
                    f"Instance profile '{profile.name}': "
                    f"role '{profile.role}' must trust EC2."
                )

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> IamProps:
        if raw is None:
            return cls()
        data = _mapping(raw, "iam", {"roles", "instance_profiles"})
        roles = data.get("roles", [])
        profiles = data.get("instance_profiles", [])
        if not isinstance(roles, list) or not isinstance(profiles, list):
            raise ValueError("iam.roles and iam.instance_profiles must be lists.")
        return cls(
            roles=tuple(
                IamRoleProps.from_dict(role, base_dir=base_dir) for role in roles
            ),
            instance_profiles=tuple(
                InstanceProfileProps.from_dict(profile) for profile in profiles
            ),
        )


class Iam(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: Common,
        env_name: str,
        props: IamProps,
    ) -> None:
        super().__init__(scope, construct_id)
        self.roles: dict[str, iam.IRole] = {}
        self.instance_profiles: dict[str, iam.CfnInstanceProfile] = {}
        for entries, suffix, limit in (
            (props.roles, "role", 64),
            (props.instance_profiles, "profile", 128),
        ):
            for entry in entries:
                name = common.name(env_name, entry.name, suffix)
                if len(name) > limit or not re.fullmatch(
                    r"[\w+=,.@-]+", name, flags=re.ASCII
                ):
                    raise ValueError(
                        f"IAM {suffix} name '{name}' must use IAM name characters "
                        f"and be at most {limit} characters."
                    )

        for role in props.roles:
            name = common.name(env_name, role.name, "role")
            policies = [
                iam.ManagedPolicy.from_aws_managed_policy_name(policy)
                for policy in role.managed_policies
            ] + [
                iam.ManagedPolicy.from_managed_policy_arn(
                    self, f"{role.name}ManagedPolicy{index}", arn
                )
                for index, arn in enumerate(role.managed_policy_arns)
            ]
            custom_policies = [
                iam.CfnRole.PolicyProperty(policy_name=key, policy_document=document)
                for key, document in role.custom_policies.items()
            ]
            resource: iam.CfnRole | iam.Role
            if role.trust_policy is not None:
                # Keep the L2's Resource path so switching trust formats does
                # not change the CloudFormation role's logical ID.
                role_scope = Construct(self, f"{role.name}Role")
                resource = iam.CfnRole(
                    role_scope, "Resource",
                    role_name=name, description=role.description,
                    assume_role_policy_document=role.trust_policy,
                    managed_policy_arns=[
                        policy.managed_policy_arn for policy in policies
                    ] or None,
                    policies=custom_policies or None,
                )
                self.roles[role.name] = iam.Role.from_role_arn(
                    self, f"{role.name}RoleReference", resource.attr_arn
                )
            else:
                if role.assumed_by is None:
                    raise ValueError(
                        f"IAM role '{role.name}': a trust configuration is required."
                    )
                resource = iam.Role(
                    self,
                    f"{role.name}Role",
                    role_name=name,
                    description=role.description,
                    assumed_by=iam.ServicePrincipal(role.assumed_by),
                    managed_policies=policies,
                )
                if custom_policies:
                    cfn_role = resource.node.default_child
                    if not isinstance(cfn_role, iam.CfnRole):
                        raise TypeError(
                            f"IAM role '{role.name}': expected an IAM CfnRole resource."
                        )
                    # Preserve the document instead of re-encoding it through
                    # CDK's L2 PolicyDocument.
                    cfn_role.policies = custom_policies
                self.roles[role.name] = resource
            cdk.Tags.of(resource).add("Name", name)

        for profile in props.instance_profiles:
            self.instance_profiles[profile.name] = iam.CfnInstanceProfile(
                self,
                f"{profile.name}Profile",
                instance_profile_name=common.name(env_name, profile.name, "profile"),
                roles=[self.roles[profile.role].role_name],
            )
