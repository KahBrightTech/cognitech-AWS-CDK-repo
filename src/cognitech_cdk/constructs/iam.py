"""Config-driven IAM: roles, instance profiles, customer-managed policies and users."""

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
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common.config import Common
from cognitech_cdk.common.documents import placeholders, render
from cognitech_cdk.constructs.lookups import sso_role_arn

# Terraform IAM modules replace these in policy files; admin_role/network_role
# resolve the account's IAM Identity Center roles at deploy time.
PLACEHOLDERS = frozenset({
    "account_number", "account_name", "account_name_abr", "region",
    "region_prefix", "partition", "admin_role", "network_role",
})
_SSO_PERMISSION_SETS = {
    "admin_role": "AdministratorAccess", "network_role": "NetworkAdministrator",
}
_POLICY_ARN = re.compile(
    r"arn:[a-z0-9-]+:iam::(?:[0-9]{12}|aws):policy/(?:[\w+=,.@-]+/)*[\w+=,.@-]+",
    flags=re.ASCII,
)
_IAM_PATH = re.compile(r"/(?:[\x21-\x7E]+/)?", flags=re.ASCII)
_REMOVAL_POLICIES = {"retain": cdk.RemovalPolicy.RETAIN, "destroy": cdk.RemovalPolicy.DESTROY}


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
    raw: Any,
    label: str,
    *,
    trust: bool = False,
    resource: bool = False,
    base_dir: Path | None = None,
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
        if resource:
            if "NotPrincipal" in statement or "Principal" not in statement:
                raise ValueError(f"{where}: resource policies must specify Principal.")
            principal = statement["Principal"]
            if principal != "*":
                principal = _mapping(
                    principal, f"{where}: Principal",
                    {"AWS", "Service", "Federated", "CanonicalUser"},
                )
                if not principal:
                    raise ValueError(f"{where}: Principal must not be empty.")
                for kind, value in principal.items():
                    _strings(value, f"{where}: Principal.{kind}")
            public = principal == "*" or (
                "AWS" in principal
                and "*" in _strings(principal["AWS"], f"{where}: Principal.AWS")
            )
            if public and statement["Effect"] == "Allow" and "Condition" not in statement:
                raise ValueError(
                    f"{where}: a public Principal requires a restrictive Condition."
                )
            for resource_key in ("Resource", "NotResource"):
                if resource_key in statement:
                    _strings(statement[resource_key], f"{where}: {resource_key}")
        elif trust:
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
    unsupported = sorted(placeholders(document) - PLACEHOLDERS)
    if unsupported:
        raise ValueError(
            f"{label}: unsupported placeholders: {', '.join(unsupported)}. "
            f"Supported: {', '.join(sorted(PLACEHOLDERS))}."
        )
    try:
        return json.loads(json.dumps(document, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{label}: policy must contain JSON-compatible values: {error}"
        ) from error


def _description(value: Any, owner: str, *, limit: int = 1000) -> str | None:
    if value is not None and (
        not isinstance(value, str) or not 1 <= len(value) <= limit
    ):
        raise ValueError(f"{owner}: description must contain 1-{limit} characters.")
    return value


def _iam_path(value: Any, owner: str) -> str | None:
    if value is not None and (
        not isinstance(value, str) or len(value) > 512 or not _IAM_PATH.fullmatch(value)
    ):
        raise ValueError(
            f"{owner}: path must be '/' or begin and end with '/' "
            "(printable ASCII, at most 512 characters)."
        )
    return value


def _aws_managed_policies(value: Any, owner: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(policy, str)
        or not re.fullmatch(r"(?:[\w+=,.@-]+/)*[\w+=,.@-]+", policy, flags=re.ASCII)
        for policy in value
    ):
        raise ValueError(
            f"{owner}: managed_policies must list AWS-managed policy names, not ARNs."
        )
    if len(set(value)) != len(value):
        raise ValueError(f"{owner}: duplicate managed policy.")
    return tuple(value)


def _managed_policy_arns(
    value: Any, owner: str, aws_managed: tuple[str, ...]
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(arn, str) or not _POLICY_ARN.fullmatch(arn) for arn in value
    ):
        raise ValueError(f"{owner}: managed_policy_arns must list IAM policy ARNs.")
    if len(set(value)) != len(value) or any(
        ":iam::aws:policy/" in arn and arn.split(":policy/", 1)[1] in aws_managed
        for arn in value
    ):
        raise ValueError(f"{owner}: duplicate managed policy.")
    return tuple(value)


def _customer_managed_policies(value: Any, owner: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(
            f"{owner}: customer_managed_policies must list iam.policies names."
        )
    names = tuple(
        _name(policy, f"{owner}: customer_managed_policies entry") for policy in value
    )
    if len({policy.casefold() for policy in names}) != len(names):
        raise ValueError(f"{owner}: duplicate customer-managed policy.")
    return names


def _permissions_boundary(value: Any, owner: str) -> str | None:
    if value is not None and (
        not isinstance(value, str)
        or not (_POLICY_ARN.fullmatch(value) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value))
    ):
        raise ValueError(
            f"{owner}: permissions_boundary must be an IAM policy ARN "
            "or an iam.policies name."
        )
    return value


@dataclass(frozen=True)
class IamRoleProps:
    name: str
    assumed_by: str | None = None
    description: str | None = None
    managed_policies: tuple[str, ...] = ()
    managed_policy_arns: tuple[str, ...] = ()
    custom_policies: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    trust_policy: Mapping[str, Any] | None = None
    path: str | None = None
    max_session_duration: int | None = None
    permissions_boundary: str | None = None
    customer_managed_policies: tuple[str, ...] = ()

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
                "managed_policy_arns", "custom_policies", "trust_policy", "path",
                "max_session_duration", "permissions_boundary",
                "customer_managed_policies",
            }
        )
        name = _name(data.get("name"), "IAM role name")
        owner = f"IAM role '{name}'"
        if ("assumed_by" in data) == ("trust_policy" in data):
            raise ValueError(
                f"{owner}: provide exactly one of assumed_by or trust_policy."
            )
        service = data.get("assumed_by")
        if "assumed_by" in data and (
            not isinstance(service, str) or not re.fullmatch(
                r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.amazonaws\.com(?:\.cn)?", service
            )
        ):
            raise ValueError(
                f"{owner}: assumed_by must be an AWS service, e.g. ec2.amazonaws.com."
            )
        description = _description(data.get("description"), owner)
        policies = _aws_managed_policies(data.get("managed_policies", []), owner)
        arns = _managed_policy_arns(data.get("managed_policy_arns", []), owner, policies)
        duration = data.get("max_session_duration")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, int)
            or not 3600 <= duration <= 43200
        ):
            raise ValueError(
                f"{owner}: max_session_duration must be 3600-43200 seconds."
            )
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
            managed_policies=policies, managed_policy_arns=arns,
            custom_policies=custom_policies, trust_policy=trust_policy,
            path=_iam_path(data.get("path"), owner),
            max_session_duration=duration,
            permissions_boundary=_permissions_boundary(
                data.get("permissions_boundary"), owner
            ),
            customer_managed_policies=_customer_managed_policies(
                data.get("customer_managed_policies", []), owner
            ),
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
class ManagedPolicyProps:
    """A customer-managed policy, mirroring the IAM-Policies module."""

    name: str
    document: Mapping[str, Any]
    description: str | None = None
    path: str | None = None

    @classmethod
    def from_dict(
        cls, raw: Any, *, base_dir: Path | None = None, label: str = "IAM policy"
    ) -> ManagedPolicyProps:
        data = _mapping(raw, label, {"name", "description", "path", "document"})
        return cls._parse(data, _name(data.get("name"), f"{label} name"), label, base_dir)

    @classmethod
    def _parse(
        cls, data: Mapping[str, Any], name: str, label: str, base_dir: Path | None
    ) -> ManagedPolicyProps:
        owner = f"{label} '{name}'"
        if "document" not in data:
            raise ValueError(f"{owner}: document is required.")
        return cls(
            name=name,
            document=_policy_document(
                data["document"], f"{owner}, document", base_dir=base_dir
            ),
            description=_description(data.get("description"), owner),
            path=_iam_path(data.get("path"), owner),
        )


@dataclass(frozen=True)
class GroupPolicyProps:
    group: str
    policy: ManagedPolicyProps

    @classmethod
    def from_dict(
        cls, raw: Any, *, owner: str, base_dir: Path | None = None
    ) -> GroupPolicyProps:
        label = f"{owner}, group policy"
        data = _mapping(
            raw, label, {"group_name", "policy_name", "description", "path", "document"}
        )
        group = _name(data.get("group_name"), f"{label} group_name")
        name = _name(data.get("policy_name"), f"{label} policy_name")
        return cls(group, ManagedPolicyProps._parse(data, name, label, base_dir))


@dataclass(frozen=True)
class UserSecretProps:
    """Secrets Manager secret holding a user's generated access key."""

    name: str | None = None
    description: str | None = None
    removal_policy: str = "retain"
    policy: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(
        cls, raw: Any, *, owner: str, base_dir: Path | None = None
    ) -> UserSecretProps:
        label = f"{owner}, secrets_manager"
        data = _mapping(raw, label, {"name", "description", "removal_policy", "policy"})
        name = data.get("name")
        if name is not None:
            _name(name, f"{label} name")
        removal = data.get("removal_policy", "retain")
        if removal not in _REMOVAL_POLICIES:
            raise ValueError(f"{label}: removal_policy must be retain or destroy.")
        return cls(
            name=name,
            description=_description(data.get("description"), label, limit=2048),
            removal_policy=removal,
            policy=(
                _policy_document(
                    data["policy"], f"{label}, policy", resource=True, base_dir=base_dir
                )
                if "policy" in data else None
            ),
        )


@dataclass(frozen=True)
class IamUserProps:
    """An IAM user, mirroring the IAM-User module."""

    name: str
    path: str | None = None
    permissions_boundary: str | None = None
    groups: tuple[str, ...] = ()
    managed_policies: tuple[str, ...] = ()
    managed_policy_arns: tuple[str, ...] = ()
    customer_managed_policies: tuple[str, ...] = ()
    policy: ManagedPolicyProps | None = None
    group_policies: tuple[GroupPolicyProps, ...] = ()
    create_access_key: bool = False
    secrets_manager: UserSecretProps | None = None
    # Accepted for Terraform parity; the IAM-User module creates nothing from these.
    description: str | None = None
    notifications_email: str | None = None
    regions: tuple[str, ...] = ()
    user_type: str = "standard"

    def __post_init__(self) -> None:
        owner = f"IAM user '{self.name}'"
        if self.secrets_manager is not None and not self.create_access_key:
            raise ValueError(f"{owner}: secrets_manager requires create_access_key: true.")
        groups = [group.casefold() for group in self.groups]
        if len(groups) != len(set(groups)):
            raise ValueError(f"{owner}: duplicate group.")
        for group_policy in self.group_policies:
            if group_policy.group not in self.groups:
                raise ValueError(
                    f"{owner}: group policy '{group_policy.policy.name}' targets "
                    f"group '{group_policy.group}', which is not in groups."
                )

    @property
    def secret_name(self) -> str:
        secret = self.secrets_manager
        return secret.name if secret is not None and secret.name else self.name

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> IamUserProps:
        data = _mapping(
            raw, "IAM user", {
                "name", "path", "permissions_boundary", "groups", "managed_policies",
                "managed_policy_arns", "customer_managed_policies", "policy",
                "group_policies", "create_access_key", "secrets_manager",
                "description", "notifications_email", "regions", "user_type",
            }
        )
        name = _name(data.get("name"), "IAM user name")
        owner = f"IAM user '{name}'"
        groups = data.get("groups", [])
        if not isinstance(groups, list):
            raise ValueError(f"{owner}: groups must be a list of group names.")
        group_policies = data.get("group_policies", [])
        if not isinstance(group_policies, list):
            raise ValueError(f"{owner}: group_policies must be a list.")
        create_key = data.get("create_access_key", False)
        if not isinstance(create_key, bool):
            raise ValueError(f"{owner}: create_access_key must be true or false.")
        email = data.get("notifications_email")
        if email is not None and (
            not isinstance(email, str)
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            raise ValueError(f"{owner}: notifications_email must be an email address.")
        regions = data.get("regions", [])
        if not isinstance(regions, list) or any(
            not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region)
            for region in regions
        ):
            raise ValueError(f"{owner}: regions must list AWS region codes.")
        user_type = data.get("user_type", "standard")
        if user_type not in ("standard", "service-linked"):
            raise ValueError(f"{owner}: user_type must be standard or service-linked.")
        policies = _aws_managed_policies(data.get("managed_policies", []), owner)
        return cls(
            name=name,
            path=_iam_path(data.get("path"), owner),
            permissions_boundary=_permissions_boundary(
                data.get("permissions_boundary"), owner
            ),
            groups=tuple(_name(group, f"{owner}: group name") for group in groups),
            managed_policies=policies,
            managed_policy_arns=_managed_policy_arns(
                data.get("managed_policy_arns", []), owner, policies
            ),
            customer_managed_policies=_customer_managed_policies(
                data.get("customer_managed_policies", []), owner
            ),
            policy=(
                ManagedPolicyProps.from_dict(
                    data["policy"], base_dir=base_dir, label=f"{owner}, policy"
                )
                if "policy" in data else None
            ),
            group_policies=tuple(
                GroupPolicyProps.from_dict(entry, owner=owner, base_dir=base_dir)
                for entry in group_policies
            ),
            create_access_key=create_key,
            secrets_manager=(
                UserSecretProps.from_dict(
                    data["secrets_manager"], owner=owner, base_dir=base_dir
                )
                if "secrets_manager" in data else None
            ),
            description=_description(data.get("description"), owner),
            notifications_email=email,
            regions=tuple(regions),
            user_type=user_type,
        )


@dataclass(frozen=True)
class IamProps:
    roles: tuple[IamRoleProps, ...] = ()
    instance_profiles: tuple[InstanceProfileProps, ...] = ()
    policies: tuple[ManagedPolicyProps, ...] = ()
    users: tuple[IamUserProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.roles or self.policies or self.users)

    @property
    def all_policies(self) -> tuple[ManagedPolicyProps, ...]:
        """`iam.policies` followed by the policies users declare inline."""
        return (
            self.policies
            + tuple(user.policy for user in self.users if user.policy is not None)
            + tuple(entry.policy for user in self.users for entry in user.group_policies)
        )

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(group for user in self.users for group in user.groups))

    def __post_init__(self) -> None:
        for label, entries in (
            ("role", self.roles), ("instance profile", self.instance_profiles),
            ("policy", self.all_policies), ("user", self.users),
        ):
            names = [entry.name.casefold() for entry in entries]
            if len(names) != len(set(names)):
                raise ValueError(
                    f"IAM: duplicate {label} name (names are case-insensitive in AWS)."
                )
        groups = [group.casefold() for group in self.groups]
        if len(groups) != len(set(groups)):
            raise ValueError(
                "IAM: group names differ only by case (names are case-insensitive in AWS)."
            )
        secrets = [
            user.secret_name.casefold() for user in self.users if user.create_access_key
        ]
        if len(secrets) != len(set(secrets)):
            raise ValueError("IAM: duplicate user credentials secret name.")
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
        known = {policy.name for policy in self.policies}
        for owner, entry in (
            *((f"IAM role '{role.name}'", role) for role in self.roles),
            *((f"IAM user '{user.name}'", user) for user in self.users),
        ):
            references = list(entry.customer_managed_policies)
            boundary = entry.permissions_boundary
            if boundary is not None and not boundary.startswith("arn:"):
                references.append(boundary)
            for reference in references:
                if reference not in known:
                    raise ValueError(
                        f"{owner}: unknown customer-managed policy '{reference}'. "
                        f"Configured iam.policies: {', '.join(sorted(known)) or 'none'}."
                    )

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> IamProps:
        if raw is None:
            return cls()
        data = _mapping(raw, "iam", {"roles", "instance_profiles", "policies", "users"})
        roles = data.get("roles", [])
        profiles = data.get("instance_profiles", [])
        if not isinstance(roles, list) or not isinstance(profiles, list):
            raise ValueError("iam.roles and iam.instance_profiles must be lists.")
        policies = data.get("policies", [])
        users = data.get("users", [])
        for key, value in (("policies", policies), ("users", users)):
            if not isinstance(value, list):
                raise ValueError(f"iam.{key} must be a list.")
        return cls(
            roles=tuple(
                IamRoleProps.from_dict(role, base_dir=base_dir) for role in roles
            ),
            instance_profiles=tuple(
                InstanceProfileProps.from_dict(profile) for profile in profiles
            ),
            policies=tuple(
                ManagedPolicyProps.from_dict(policy, base_dir=base_dir)
                for policy in policies
            ),
            users=tuple(IamUserProps.from_dict(user, base_dir=base_dir) for user in users),
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
        self._common = common
        self._sso_roles: dict[str, str] = {}
        self.roles: dict[str, iam.IRole] = {}
        # CloudFormation role names; differs from `roles[...].role_name` only for
        # imported references of roles with a path.
        self.role_names: dict[str, str] = {}
        self.instance_profiles: dict[str, iam.CfnInstanceProfile] = {}
        self.managed_policies: dict[str, iam.IManagedPolicy] = {}
        self.managed_policy_names: dict[str, str] = {}
        self.users: dict[str, iam.IUser] = {}
        self.groups: dict[str, iam.IGroup] = {}
        self.user_secrets: dict[str, secretsmanager.ISecret] = {}
        for entries, suffix, limit in (
            (props.roles, "role", 64),
            (props.instance_profiles, "profile", 128),
            (props.all_policies, "policy", 128),
            (props.users, "user", 64),
        ):
            for entry in entries:
                self._check_name(common.name(env_name, entry.name, suffix), suffix, limit)
        for group in props.groups:
            self._check_name(common.name(env_name, group, "group"), "group", 128)

        for policy in props.all_policies:
            name = common.name(env_name, policy.name, "policy")
            resource = iam.CfnManagedPolicy(
                self,
                f"{policy.name}Policy",
                managed_policy_name=name,
                description=policy.description,
                path=policy.path,
                policy_document=self._render(
                    policy.document, f"IAM policy '{policy.name}'"
                ),
            )
            self.managed_policies[policy.name] = iam.ManagedPolicy.from_managed_policy_arn(
                self, f"{policy.name}PolicyReference", resource.ref
            )
            self.managed_policy_names[policy.name] = name

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
            ] + [
                self.managed_policies[policy] for policy in role.customer_managed_policies
            ]
            boundary = self._boundary(role.permissions_boundary, f"{role.name}Role")
            custom_policies = [
                iam.CfnRole.PolicyProperty(
                    policy_name=key,
                    policy_document=self._render(
                        document, f"IAM role '{role.name}', custom policy '{key}'"
                    ),
                )
                for key, document in role.custom_policies.items()
            ]
            trust_policy = (
                self._render(role.trust_policy, f"IAM role '{role.name}', trust_policy")
                if role.trust_policy is not None else None
            )
            resource: iam.CfnRole | iam.Role
            if trust_policy is not None and role.path is None:
                # Keep the L2's Resource path so switching trust formats does
                # not change the CloudFormation role's logical ID.
                role_scope = Construct(self, f"{role.name}Role")
                resource = iam.CfnRole(
                    role_scope, "Resource",
                    role_name=name, description=role.description,
                    assume_role_policy_document=trust_policy,
                    managed_policy_arns=[
                        policy.managed_policy_arn for policy in policies
                    ] or None,
                    policies=custom_policies or None,
                    max_session_duration=role.max_session_duration,
                    permissions_boundary=(
                        boundary.managed_policy_arn if boundary is not None else None
                    ),
                )
                self.roles[role.name] = iam.Role.from_role_arn(
                    self, f"{role.name}RoleReference", resource.attr_arn
                )
                self.role_names[role.name] = self.roles[role.name].role_name
            else:
                if role.assumed_by is None and trust_policy is None:
                    raise ValueError(
                        f"IAM role '{role.name}': a trust configuration is required."
                    )
                # An imported reference cannot derive the name of a role with a
                # path from its ARN, so pathed custom-trust roles use the L2
                # with the configured document replacing its trust policy.
                resource = iam.Role(
                    self,
                    f"{role.name}Role",
                    role_name=name,
                    description=role.description,
                    assumed_by=(
                        iam.ServicePrincipal(role.assumed_by)
                        if role.assumed_by is not None else iam.AccountRootPrincipal()
                    ),
                    managed_policies=policies,
                    path=role.path,
                    max_session_duration=(
                        cdk.Duration.seconds(role.max_session_duration)
                        if role.max_session_duration is not None else None
                    ),
                    permissions_boundary=boundary,
                )
                cfn_role = resource.node.default_child
                if not isinstance(cfn_role, iam.CfnRole):
                    raise TypeError(
                        f"IAM role '{role.name}': expected an IAM CfnRole resource."
                    )
                if trust_policy is not None:
                    cfn_role.assume_role_policy_document = trust_policy
                if custom_policies:
                    # Preserve the document instead of re-encoding it through
                    # CDK's L2 PolicyDocument.
                    cfn_role.policies = custom_policies
                self.roles[role.name] = resource
                self.role_names[role.name] = resource.role_name
            cdk.Tags.of(resource).add("Name", name)

        for profile in props.instance_profiles:
            self.instance_profiles[profile.name] = iam.CfnInstanceProfile(
                self,
                f"{profile.name}Profile",
                instance_profile_name=common.name(env_name, profile.name, "profile"),
                roles=[self.role_names[profile.role]],
            )

        for group in props.groups:
            self.groups[group] = iam.Group(
                self,
                f"{group}Group",
                group_name=common.name(env_name, group, "group"),
                managed_policies=[
                    self.managed_policies[entry.policy.name]
                    for user in props.users
                    for entry in user.group_policies
                    if entry.group == group
                ] or None,
            )

        for user in props.users:
            self._user(user, common.name(env_name, user.name, "user"), env_name)

    def _user(self, user: IamUserProps, name: str, env_name: str) -> None:
        policies = [
            iam.ManagedPolicy.from_aws_managed_policy_name(policy)
            for policy in user.managed_policies
        ] + [
            iam.ManagedPolicy.from_managed_policy_arn(
                self, f"{user.name}UserManagedPolicy{index}", arn
            )
            for index, arn in enumerate(user.managed_policy_arns)
        ] + [
            self.managed_policies[policy] for policy in user.customer_managed_policies
        ] + ([self.managed_policies[user.policy.name]] if user.policy else [])
        resource = iam.User(
            self,
            f"{user.name}User",
            user_name=name,
            path=user.path,
            permissions_boundary=self._boundary(
                user.permissions_boundary, f"{user.name}User"
            ),
            managed_policies=policies or None,
            groups=[self.groups[group] for group in user.groups] or None,
        )
        cdk.Tags.of(resource).add("Name", name)
        self.users[user.name] = resource
        if not user.create_access_key:
            return

        settings = user.secrets_manager or UserSecretProps()
        key = iam.AccessKey(self, f"{user.name}AccessKey", user=resource)
        secret_name = self._common.name(env_name, user.secret_name, "credentials")
        if len(secret_name) > 512:
            raise ValueError(
                f"Secrets Manager secret name '{secret_name}' must be at most 512 characters."
            )
        # Only CloudFormation references reach the template: the secret access
        # key is resolved by CloudFormation and never appears in outputs.
        secret = secretsmanager.Secret(
            self,
            f"{user.name}Credentials",
            secret_name=secret_name,
            description=(
                settings.description or f"Access credentials for IAM user {user.name}"
            ),
            secret_object_value={
                "access_key_id": cdk.SecretValue.unsafe_plain_text(key.access_key_id),
                "secret_access_key": key.secret_access_key,
                "username": cdk.SecretValue.unsafe_plain_text(resource.user_name),
            },
            removal_policy=_REMOVAL_POLICIES[settings.removal_policy],
        )
        cdk.Tags.of(secret).add("Name", secret_name)
        if settings.policy is not None:
            secretsmanager.CfnResourcePolicy(
                self,
                f"{user.name}CredentialsPolicy",
                secret_id=secret.secret_arn,
                resource_policy=self._render(
                    settings.policy, f"IAM user '{user.name}', secrets_manager policy"
                ),
                block_public_policy=True,
            )
        self.user_secrets[user.name] = secret

    @staticmethod
    def _check_name(name: str, suffix: str, limit: int) -> None:
        if len(name) > limit or not re.fullmatch(r"[\w+=,.@-]+", name, flags=re.ASCII):
            raise ValueError(
                f"IAM {suffix} name '{name}' must use IAM name characters "
                f"and be at most {limit} characters."
            )

    def _boundary(self, reference: str | None, owner_id: str) -> iam.IManagedPolicy | None:
        if reference is None:
            return None
        if reference in self.managed_policies:
            return self.managed_policies[reference]
        return iam.ManagedPolicy.from_managed_policy_arn(
            self, f"{owner_id}PermissionsBoundary", reference
        )

    def _render(self, document: Mapping[str, Any], label: str) -> Mapping[str, Any]:
        """Resolve Terraform-style `[[placeholders]]`; documents without any are untouched."""
        needed = placeholders(document)
        if not needed:
            return document
        stack = cdk.Stack.of(self)
        values: dict[str, str] = {
            "account_number": stack.account,
            "region": stack.region,
            "partition": stack.partition,
            "account_name": self._common.account_name,
            "account_name_abr": self._common.account_name_abr,
            "region_prefix": self._common.region_prefix,
        }
        for key in needed & _SSO_PERMISSION_SETS.keys():
            if key not in self._sso_roles:
                permission_set = _SSO_PERMISSION_SETS[key]
                self._sso_roles[key] = sso_role_arn(
                    self, f"{permission_set}SsoRoleLookup", permission_set
                )
            values[key] = self._sso_roles[key]
        return render(document, values, label)
