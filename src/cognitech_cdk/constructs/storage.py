"""Private S3 buckets, mirroring the Terraform `S3-Private-bucket` module.

Every bucket blocks all public access, enforces TLS, encrypts at rest, uses
bucket-owner-enforced object ownership and is retained on stack deletion unless
the YAML says otherwise. Optional pieces: a bucket policy (embedded or a
repository-relative JSON/YAML file with Terraform-style `[[placeholders]]`),
empty marker objects such as `Ansible_Tower/`, and cross-bucket replication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import custom_resources as cr
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.config import Common
from cognitech_cdk.common.documents import (
    PLACEHOLDER,
    json_compatible,
    load_document,
    placeholders,
    render,
)
from cognitech_cdk.constructs.lookups import sso_role_arn

BUCKET_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
KMS_KEY_ARN = re.compile(r"arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+")
KMS_ALIAS_ARN = re.compile(r"arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:alias/[A-Za-z0-9/_-]+")
KMS_KEY_ID = re.compile(r"(?:mrk-[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
KMS_ALIAS_NAME = re.compile(r"alias/[A-Za-z0-9/_-]+")
ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:role/(?:[\w+=,.@-]+/)*[\w+=,.@-]+", re.ASCII)
S3_BUCKET_ARN = re.compile(r"arn:aws[a-z-]*:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")

# Placeholders the stack fills in; `policy_variables` cannot redefine them.
BUILTIN_PLACEHOLDERS = frozenset({
    "resource_name", "bucket_name", "bucket_arn", "account_number", "region",
    "partition", "account_name", "region_prefix", "admin_role", "network_role",
})
SSO_PLACEHOLDERS = {"admin_role": "AdministratorAccess", "network_role": "NetworkAdministrator"}

OBJECT_OWNERSHIP = {
    "bucket_owner_enforced": s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
    "bucket_owner_preferred": s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
    "object_writer": s3.ObjectOwnership.OBJECT_WRITER,
}
REMOVAL_POLICIES = {"retain": cdk.RemovalPolicy.RETAIN, "destroy": cdk.RemovalPolicy.DESTROY}
STORAGE_CLASSES = frozenset({
    "STANDARD", "REDUCED_REDUNDANCY", "STANDARD_IA", "ONEZONE_IA",
    "INTELLIGENT_TIERING", "GLACIER", "DEEP_ARCHIVE", "OUTPOSTS", "GLACIER_IR",
})

# Condition keys that tie a `Principal: "*"` Allow to specific identities.
_RESTRICTING_KEYS = frozenset({
    "aws:principalarn", "aws:principalaccount", "aws:principalorgid",
    "aws:principalorgpaths", "aws:sourcearn", "aws:sourceaccount",
    "aws:sourceowner", "aws:sourcevpc", "aws:sourcevpce", "aws:sourceorgid",
    "aws:sourceorgpaths",
})
_ACCOUNT_KEYS = frozenset({"aws:principalaccount", "aws:sourceaccount", "aws:sourceowner"})
_RESTRICTING_OPERATORS = frozenset({
    "stringequals", "stringequalsignorecase", "stringlike", "arnequals", "arnlike",
})


def bucket_logical_name(name: str) -> str:
    """Alphanumeric construct/output id fragment for a bucket config name."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name))


# --- policy validation ------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _is_public(principal: Any) -> bool:
    if principal == "*":
        return True
    return isinstance(principal, Mapping) and "*" in _as_list(principal.get("AWS", []))


def _restrictive_value(key: str, operator: str, value: Any) -> bool:
    if not isinstance(value, str) or not value.strip("*? "):
        return False
    if operator.endswith("like"):
        if key in _ACCOUNT_KEYS and any(char in value for char in "*?"):
            return False
        parts = value.split(":")
        if value.startswith("arn:") and len(parts) > 4 and any(char in parts[4] for char in "*?"):
            return False
    return True


def _has_restrictive_condition(condition: Mapping[str, Any]) -> bool:
    for raw_operator, values in condition.items():
        operator = raw_operator.lower()
        # ForAllValues is vacuously true when the key is absent.
        if operator.startswith("forallvalues:") or operator.endswith("ifexists"):
            continue
        operator = operator.removeprefix("foranyvalue:")
        if operator not in _RESTRICTING_OPERATORS:
            continue
        for raw_key, value in values.items():
            key = str(raw_key).lower()
            entries = _as_list(value)
            if key in _RESTRICTING_KEYS and entries and all(
                _restrictive_value(key, operator, entry) for entry in entries
            ):
                return True
    return False


def _validate_policy(document: Any, label: str) -> None:
    """A bucket resource policy that never grants access to the public."""
    data = schema.mapping(document, label, {"Version", "Id", "Statement"}, required=("Statement",))
    if "Version" in data and data["Version"] not in ("2012-10-17", "2008-10-17"):
        raise ValueError(f"{label}: Version must be a quoted IAM version string.")
    if "Id" in data and not isinstance(data["Id"], str):
        raise ValueError(f"{label}: Id must be a string.")
    statements = data["Statement"]
    if isinstance(statements, Mapping):
        statements = [statements]
    if not isinstance(statements, list) or not statements:
        raise ValueError(f"{label}: Statement must be a statement or a nonempty list.")
    for index, raw in enumerate(statements, start=1):
        where = f"{label}, statement {index}"
        statement = schema.mapping(raw, where, {
            "Sid", "Effect", "Action", "NotAction", "Resource", "NotResource",
            "Principal", "NotPrincipal", "Condition",
        })
        if statement.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f"{where}: Effect must be Allow or Deny.")
        if "Sid" in statement and not isinstance(statement["Sid"], str):
            raise ValueError(f"{where}: Sid must be a string.")
        for first, second in (("Action", "NotAction"), ("Resource", "NotResource"), ("Principal", "NotPrincipal")):
            if (first in statement) == (second in statement):
                raise ValueError(f"{where}: provide exactly one of {first} or {second}.")
        for key in ("Action", "NotAction", "Resource", "NotResource"):
            if key in statement:
                schema.strings(statement[key], f"{where}: {key}", allow_empty=False)
        principal = statement.get("Principal", statement.get("NotPrincipal"))
        if principal != "*":
            principal = schema.mapping(
                principal, f"{where}: Principal", {"AWS", "Service", "Federated", "CanonicalUser"}
            )
            if not principal:
                raise ValueError(f"{where}: Principal must not be empty.")
            for kind, value in principal.items():
                schema.strings(value, f"{where}: Principal.{kind}", allow_empty=False)
        condition = statement.get("Condition", {})
        if not isinstance(condition, Mapping) or ("Condition" in statement and not condition) or any(
            not isinstance(operator, str) or not isinstance(values, Mapping) or not values
            for operator, values in condition.items()
        ):
            raise ValueError(f"{where}: Condition must map operators to condition mappings.")
        if statement["Effect"] == "Allow":
            if "NotPrincipal" in statement:
                raise ValueError(f"{where}: Allow with NotPrincipal would grant access to everyone else.")
            if _is_public(principal) and not _has_restrictive_condition(condition):
                raise ValueError(
                    f"{where}: Allow to Principal '*' needs a restrictive condition "
                    "(e.g. StringEquals aws:PrincipalArn, aws:SourceAccount or aws:SourceArn); "
                    "public bucket policies are not allowed."
                )


# --- props ------------------------------------------------------------------


@dataclass(frozen=True)
class BucketEncryptionProps:
    """Terraform `encryption`: SSE-S3 (`AES256`) or SSE-KMS (`aws:kms`)."""

    sse_algorithm: str = "AES256"
    kms_master_key_id: str | None = None
    bucket_key_enabled: bool = False

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> BucketEncryptionProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, label, {"enabled", "sse_algorithm", "kms_master_key_id", "bucket_key_enabled"})
        if not schema.boolean(data.get("enabled"), f"{label}.enabled", default=True):
            raise ValueError(f"{label}.enabled: encryption cannot be disabled; S3 always encrypts at rest.")
        algorithm = schema.string(data.get("sse_algorithm"), f"{label}.sse_algorithm", default="AES256")
        if algorithm not in ("AES256", "aws:kms"):
            raise ValueError(f"{label}.sse_algorithm must be 'AES256' or 'aws:kms'.")
        key = schema.string(data.get("kms_master_key_id"), f"{label}.kms_master_key_id")
        bucket_key = schema.boolean(data.get("bucket_key_enabled"), f"{label}.bucket_key_enabled")
        if algorithm == "aws:kms":
            if key is None:
                raise ValueError(f"{label}: kms_master_key_id is required when sse_algorithm is 'aws:kms'.")
            if not (KMS_KEY_ARN.fullmatch(key) or KMS_KEY_ID.fullmatch(key) or KMS_ALIAS_NAME.fullmatch(key)):
                raise ValueError(
                    f"{label}.kms_master_key_id must be a KMS key ARN, key ID or 'alias/<name>'."
                )
        else:
            if key is not None:
                raise ValueError(f"{label}.kms_master_key_id requires sse_algorithm 'aws:kms'.")
            if bucket_key:
                raise ValueError(f"{label}.bucket_key_enabled requires sse_algorithm 'aws:kms'.")
        return cls(algorithm, key, bucket_key)

    @property
    def kms(self) -> bool:
        return self.sse_algorithm == "aws:kms"


@dataclass(frozen=True)
class BucketObjectProps:
    """An empty object, typically a folder marker such as `Ansible_Tower/`."""

    key: str

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> BucketObjectProps:
        data = schema.mapping(raw, label, {"key"}, required=("key",))
        key = schema.required_string(data.get("key"), f"{label}.key")
        if key.startswith("/") or len(key.encode("utf-8")) > 1024 or any(ord(char) < 32 for char in key):
            raise ValueError(
                f"{label}.key must not start with '/', contain control characters or exceed 1024 bytes."
            )
        return cls(key)


@dataclass(frozen=True)
class ReplicationDestinationProps:
    bucket_arn: str
    storage_class: str = "STANDARD"
    account_id: str | None = None
    access_control_translation_owner: str | None = None
    replica_kms_key_id: str | None = None
    replication_time_minutes: int | None = None
    metrics_enabled: bool = False
    metrics_event_threshold_minutes: int = 15

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ReplicationDestinationProps:
        data = schema.mapping(raw, label, {
            "bucket_arn", "storage_class", "access_control_translation", "account_id",
            "encryption_configuration", "replication_time", "replica_modification",
        }, required=("bucket_arn",))
        bucket_arn = schema.required_string(data.get("bucket_arn"), f"{label}.bucket_arn")
        if not S3_BUCKET_ARN.fullmatch(bucket_arn):
            raise ValueError(f"{label}.bucket_arn must be an S3 bucket ARN.")
        storage_class = schema.string(data.get("storage_class"), f"{label}.storage_class", default="STANDARD")
        if storage_class not in STORAGE_CLASSES:
            raise ValueError(f"{label}.storage_class must be one of {', '.join(sorted(STORAGE_CLASSES))}.")
        account = data.get("account_id")
        if account is not None:
            account = str(account).zfill(12) if isinstance(account, int) and not isinstance(account, bool) else account
            if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
                raise ValueError(f"{label}.account_id must be a 12-digit AWS account ID.")
        owner = None
        if data.get("access_control_translation") is not None:
            where = f"{label}.access_control_translation"
            translation = schema.mapping(data["access_control_translation"], where, {"owner"}, required=("owner",))
            owner = translation["owner"]
            if owner != "Destination":
                raise ValueError(f"{where}.owner must be 'Destination'.")
            if account is None:
                raise ValueError(f"{where} requires account_id (the destination bucket owner).")
        replica_key = None
        if data.get("encryption_configuration") is not None:
            where = f"{label}.encryption_configuration"
            encryption = schema.mapping(
                data["encryption_configuration"], where, {"replica_kms_key_id"}, required=("replica_kms_key_id",)
            )
            replica_key = schema.required_string(encryption["replica_kms_key_id"], f"{where}.replica_kms_key_id")
            if not (KMS_KEY_ARN.fullmatch(replica_key) or KMS_ALIAS_ARN.fullmatch(replica_key)):
                raise ValueError(f"{where}.replica_kms_key_id must be a KMS key or alias ARN.")
        minutes = None
        if data.get("replication_time") is not None:
            where = f"{label}.replication_time"
            replication_time = schema.mapping(data["replication_time"], where, {"minutes"})
            minutes = schema.integer(replication_time.get("minutes"), f"{where}.minutes", default=15)
            if minutes != 15:
                raise ValueError(f"{where}.minutes must be 15 (the only S3 Replication Time Control value).")
        metrics, threshold = False, 15
        if data.get("replica_modification") is not None:
            where = f"{label}.replica_modification"
            modification = schema.mapping(
                data["replica_modification"], where, {"enabled", "metrics_event_threshold_minutes"}
            )
            metrics = schema.boolean(modification.get("enabled"), f"{where}.enabled")
            threshold = schema.integer(
                modification.get("metrics_event_threshold_minutes"),
                f"{where}.metrics_event_threshold_minutes", default=15,
            )
            if threshold != 15:
                raise ValueError(f"{where}.metrics_event_threshold_minutes must be 15.")
        if minutes is not None and not metrics:
            raise ValueError(
                f"{label}: replication_time requires replica_modification.enabled (replication metrics)."
            )
        return cls(bucket_arn, storage_class, account, owner, replica_key, minutes, metrics, threshold)


@dataclass(frozen=True)
class ReplicationRuleProps:
    destination: ReplicationDestinationProps
    status: str = "Enabled"
    prefix: str = ""
    delete_marker_replication: bool = False

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ReplicationRuleProps:
        data = schema.mapping(
            raw, label, {"status", "delete_marker_replication", "prefix", "destination"},
            required=("status", "destination"),
        )
        status = data["status"]
        if status not in ("Enabled", "Disabled"):
            raise ValueError(f"{label}.status must be 'Enabled' or 'Disabled'.")
        prefix = schema.string(data.get("prefix"), f"{label}.prefix", default="", allow_empty=True)
        return cls(
            destination=ReplicationDestinationProps.from_dict(data["destination"], f"{label}.destination"),
            status=status,
            prefix=prefix or "",
            delete_marker_replication=schema.boolean(
                data.get("delete_marker_replication"), f"{label}.delete_marker_replication"
            ),
        )


@dataclass(frozen=True)
class ReplicationProps:
    """Replication role is an `iam.roles` name (`role`) or an explicit `role_arn`."""

    rules: tuple[ReplicationRuleProps, ...]
    role: str | None = None
    role_arn: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ReplicationProps:
        data = schema.mapping(raw, label, {"role", "role_arn", "rules"}, required=("rules",))
        if ("role" in data) == ("role_arn" in data):
            raise ValueError(f"{label}: provide exactly one of role (an iam.roles name) or role_arn.")
        role = schema.name(data["role"], f"{label}.role") if "role" in data else None
        role_arn = None
        if "role_arn" in data:
            role_arn = schema.required_string(data["role_arn"], f"{label}.role_arn")
            if not ROLE_ARN.fullmatch(role_arn):
                raise ValueError(f"{label}.role_arn must be an IAM role ARN.")
        rules = data["rules"]
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"{label}.rules must be a nonempty list.")
        return cls(
            rules=tuple(
                ReplicationRuleProps.from_dict(rule, f"{label}.rules[{index}]")
                for index, rule in enumerate(rules)
            ),
            role=role,
            role_arn=role_arn,
        )


@dataclass(frozen=True)
class BucketProps:
    name: str
    description: str | None = None
    versioning: bool = True
    encryption: BucketEncryptionProps = BucketEncryptionProps()
    policy: Mapping[str, Any] | None = None
    policy_variables: Mapping[str, str] = field(default_factory=dict)
    objects: tuple[BucketObjectProps, ...] = ()
    replication: ReplicationProps | None = None
    object_ownership: str = "bucket_owner_enforced"
    removal_policy: str = "retain"
    auto_delete_objects: bool = False

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> BucketProps:
        data = schema.mapping(raw, "S3 bucket", {
            "name", "description", "versioning", "encryption", "policy", "policy_variables",
            "objects", "replication", "object_ownership", "removal_policy", "auto_delete_objects",
        }, required=("name",))
        name = schema.name(data["name"], "S3 bucket name")
        label = f"S3 bucket '{name}'"
        if not BUCKET_NAME.fullmatch(name):
            raise ValueError(
                f"{label}: name must use lowercase letters, digits and '-' and start/end "
                "with a letter or digit (S3 bucket naming rules)."
            )
        description = schema.string(data.get("description"), f"{label}.description")
        versioning = schema.boolean(data.get("versioning"), f"{label}.versioning", default=True)
        encryption = BucketEncryptionProps.from_dict(data.get("encryption"), f"{label}.encryption")

        variables = schema.string_map(data.get("policy_variables"), f"{label}.policy_variables")
        for key in variables:
            if not PLACEHOLDER.fullmatch(f"[[{key}]]"):
                raise ValueError(f"{label}.policy_variables: invalid placeholder name '{key}'.")
            if key in BUILTIN_PLACEHOLDERS:
                raise ValueError(f"{label}.policy_variables: '{key}' is a built-in placeholder.")
        policy = None
        if data.get("policy") is not None:
            where = f"{label}.policy"
            policy = json_compatible(load_document(data["policy"], where, base_dir=base_dir), where)
            missing = sorted(placeholders(policy) - BUILTIN_PLACEHOLDERS - set(variables))
            if missing:
                raise ValueError(f"{where}: unresolved placeholders: {', '.join(missing)}.")
            # Validate with variables substituted so a '*' variable cannot slip through.
            preview = render(
                policy, {**{key: f"[[{key}]]" for key in BUILTIN_PLACEHOLDERS}, **variables}, where
            )
            _validate_policy(preview, where)
        elif variables:
            raise ValueError(f"{label}: policy_variables requires a policy.")

        objects = data.get("objects")
        if objects is not None and not isinstance(objects, list):
            raise ValueError(f"{label}.objects must be a list.")
        parsed_objects = tuple(
            BucketObjectProps.from_dict(item, f"{label}.objects[{index}]")
            for index, item in enumerate(objects or [])
        )
        keys = [item.key for item in parsed_objects]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{label}.objects: duplicate key.")

        replication = (
            ReplicationProps.from_dict(data["replication"], f"{label}.replication")
            if data.get("replication") is not None else None
        )
        if replication is not None and not versioning:
            raise ValueError(f"{label}: replication requires versioning.")

        ownership = schema.string(
            data.get("object_ownership"), f"{label}.object_ownership", default="bucket_owner_enforced"
        )
        if ownership not in OBJECT_OWNERSHIP:
            raise ValueError(f"{label}.object_ownership must be one of {', '.join(OBJECT_OWNERSHIP)}.")
        removal = schema.string(data.get("removal_policy"), f"{label}.removal_policy", default="retain")
        if removal not in REMOVAL_POLICIES:
            raise ValueError(f"{label}.removal_policy must be 'retain' or 'destroy'.")
        auto_delete = schema.boolean(data.get("auto_delete_objects"), f"{label}.auto_delete_objects")
        if auto_delete and removal != "destroy":
            raise ValueError(f"{label}.auto_delete_objects requires removal_policy 'destroy'.")
        return cls(
            name=name, description=description, versioning=versioning, encryption=encryption,
            policy=policy, policy_variables=variables, objects=parsed_objects,
            replication=replication, object_ownership=ownership, removal_policy=removal,
            auto_delete_objects=auto_delete,
        )


@dataclass(frozen=True)
class StorageProps:
    """The `s3_buckets` YAML list."""

    buckets: tuple[BucketProps, ...] = ()

    def __post_init__(self) -> None:
        seen: dict[str, str] = {}
        for bucket in self.buckets:
            logical = bucket_logical_name(bucket.name).casefold()
            if logical in seen:
                raise ValueError(
                    f"s3_buckets: '{bucket.name}' and '{seen[logical]}' map to the same "
                    "CloudFormation id; rename one."
                )
            seen[logical] = bucket.name

    def __bool__(self) -> bool:
        return bool(self.buckets)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> StorageProps:
        return cls(
            buckets=schema.items(
                raw, "s3_buckets", lambda item: BucketProps.from_dict(item, base_dir=base_dir)
            )
        )


# --- construct --------------------------------------------------------------


class Storage(Construct):
    """Creates the configured buckets; `buckets` is keyed by config name."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: Common,
        network_name: str,
        props: StorageProps,
        roles: Mapping[str, iam.IRole] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        self._common = common
        self._sso_roles: dict[str, str] = {}
        self.buckets: dict[str, s3.IBucket] = {}
        self.bucket_names: dict[str, str] = {}

        for bucket in props.buckets:
            name = common.name(network_name, bucket.name)
            if not 3 <= len(name) <= 63 or not BUCKET_NAME.fullmatch(name):
                raise ValueError(
                    f"S3 bucket '{bucket.name}': final name '{name}' must be 3-63 lowercase "
                    "letters, digits or '-'."
                )
            self.bucket_names[bucket.name] = name

        for bucket in props.buckets:
            self.buckets[bucket.name] = self._bucket(bucket, self.bucket_names[bucket.name], roles or {})

    def _sso_role(self, placeholder: str) -> str:
        if placeholder not in self._sso_roles:
            self._sso_roles[placeholder] = sso_role_arn(
                self, f"{bucket_logical_name(placeholder)}Lookup", SSO_PLACEHOLDERS[placeholder]
            )
        return self._sso_roles[placeholder]

    def _encryption_key(self, props: BucketProps, logical: str) -> kms.IKey | None:
        key_id = props.encryption.kms_master_key_id
        if key_id is None:
            return None
        if key_id.startswith("alias/"):
            return kms.Alias.from_alias_name(self, f"{logical}EncryptionKey", key_id)
        if not key_id.startswith("arn:"):
            key_id = cdk.Stack.of(self).format_arn(service="kms", resource="key", resource_name=key_id)
        return kms.Key.from_key_arn(self, f"{logical}EncryptionKey", key_id)

    def _bucket(self, props: BucketProps, name: str, roles: Mapping[str, iam.IRole]) -> s3.Bucket:
        logical = bucket_logical_name(props.name)
        stack = cdk.Stack.of(self)
        key = self._encryption_key(props, logical)
        destroy = props.removal_policy == "destroy"
        bucket = s3.Bucket(
            self,
            f"{logical}Bucket",
            bucket_name=name,
            versioned=props.versioning,
            encryption=s3.BucketEncryption.KMS if key is not None else s3.BucketEncryption.S3_MANAGED,
            encryption_key=key,
            bucket_key_enabled=props.encryption.bucket_key_enabled or None,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=OBJECT_OWNERSHIP[props.object_ownership],
            removal_policy=REMOVAL_POLICIES[props.removal_policy],
            auto_delete_objects=props.auto_delete_objects,
        )
        cdk.Tags.of(bucket).add("Name", name)

        if props.policy is not None:
            values: dict[str, Any] = {
                "resource_name": name,
                "bucket_name": name,
                "bucket_arn": f"arn:{stack.partition}:s3:::{name}",
                "account_number": stack.account,
                "region": stack.region,
                "partition": stack.partition,
                "account_name": self._common.account_name,
                "region_prefix": self._common.region_prefix,
                **props.policy_variables,
            }
            for placeholder in SSO_PLACEHOLDERS:
                if placeholder in placeholders(props.policy):
                    values[placeholder] = self._sso_role(placeholder)
            document = render(props.policy, values, f"S3 bucket '{props.name}'.policy")
            statements = document["Statement"]
            for statement in statements if isinstance(statements, list) else [statements]:
                bucket.add_to_resource_policy(iam.PolicyStatement.from_json(statement))

        if props.replication is not None:
            self._replication(bucket, props, roles)

        for index, item in enumerate(props.objects):
            self._object(bucket, props, logical, index, item.key, key)
        return bucket

    def _replication(self, bucket: s3.Bucket, props: BucketProps, roles: Mapping[str, iam.IRole]) -> None:
        replication = props.replication
        assert replication is not None
        if replication.role is not None:
            role_arn = schema.lookup(
                roles, replication.role, f"S3 bucket '{props.name}'.replication.role"
            ).role_arn
        else:
            role_arn = replication.role_arn
        rules = []
        for index, rule in enumerate(replication.rules):
            destination = rule.destination
            rules.append(s3.CfnBucket.ReplicationRuleProperty(
                id=f"replication-rule-{index}",
                priority=index,
                status=rule.status,
                filter=s3.CfnBucket.ReplicationRuleFilterProperty(prefix=rule.prefix),
                delete_marker_replication=s3.CfnBucket.DeleteMarkerReplicationProperty(
                    status="Enabled" if rule.delete_marker_replication else "Disabled"
                ),
                destination=s3.CfnBucket.ReplicationDestinationProperty(
                    bucket=destination.bucket_arn,
                    storage_class=destination.storage_class,
                    account=destination.account_id,
                    access_control_translation=(
                        s3.CfnBucket.AccessControlTranslationProperty(
                            owner=destination.access_control_translation_owner
                        )
                        if destination.access_control_translation_owner else None
                    ),
                    encryption_configuration=(
                        s3.CfnBucket.EncryptionConfigurationProperty(
                            replica_kms_key_id=destination.replica_kms_key_id
                        )
                        if destination.replica_kms_key_id else None
                    ),
                    replication_time=(
                        s3.CfnBucket.ReplicationTimeProperty(
                            status="Enabled",
                            time=s3.CfnBucket.ReplicationTimeValueProperty(
                                minutes=destination.replication_time_minutes
                            ),
                        )
                        if destination.replication_time_minutes is not None else None
                    ),
                    metrics=(
                        s3.CfnBucket.MetricsProperty(
                            status="Enabled",
                            event_threshold=s3.CfnBucket.ReplicationTimeValueProperty(
                                minutes=destination.metrics_event_threshold_minutes
                            ),
                        )
                        if destination.metrics_enabled else None
                    ),
                ),
                source_selection_criteria=(
                    s3.CfnBucket.SourceSelectionCriteriaProperty(
                        sse_kms_encrypted_objects=s3.CfnBucket.SseKmsEncryptedObjectsProperty(status="Enabled")
                    )
                    if destination.replica_kms_key_id else None
                ),
            ))
        cfn_bucket = bucket.node.default_child
        if not isinstance(cfn_bucket, s3.CfnBucket):
            raise TypeError(f"S3 bucket '{props.name}': expected an S3 CfnBucket resource.")
        cfn_bucket.replication_configuration = s3.CfnBucket.ReplicationConfigurationProperty(
            role=role_arn, rules=rules
        )

    def _object(
        self, bucket: s3.Bucket, props: BucketProps, logical: str, index: int, key: str, encryption_key: kms.IKey | None
    ) -> None:
        stack = cdk.Stack.of(self)
        call = cr.AwsSdkCall(
            service="S3",
            action="putObject",
            parameters={"Bucket": bucket.bucket_name, "Key": key, "Body": ""},
            physical_resource_id=cr.PhysicalResourceId.of(f"{self.bucket_names[props.name]}/{key}"),
        )
        statements = [iam.PolicyStatement(
            actions=["s3:PutObject", "s3:DeleteObject"], resources=[bucket.arn_for_objects(key)]
        )]
        if encryption_key is not None:
            alias = (props.encryption.kms_master_key_id or "").startswith("alias/")
            statements.append(iam.PolicyStatement(
                actions=["kms:GenerateDataKey", "kms:Encrypt"],
                # An alias does not name the key IAM evaluates; scope it to S3 instead.
                resources=["*" if alias else encryption_key.key_arn],
                conditions={"StringEquals": {"kms:ViaService": f"s3.{stack.region}.{stack.url_suffix}"}},
            ))
        marker = cr.AwsCustomResource(
            self,
            f"{logical}Object{index}",
            resource_type="Custom::S3Object",
            on_create=call,
            on_update=call,
            # Retained buckets keep their markers, like the bucket's other objects.
            on_delete=cr.AwsSdkCall(
                service="S3",
                action="deleteObject",
                parameters={"Bucket": bucket.bucket_name, "Key": key},
            ) if props.removal_policy == "destroy" else None,
            policy=cr.AwsCustomResourcePolicy.from_statements(statements),
            install_latest_aws_sdk=False,
        )
        if bucket.policy is not None:
            marker.node.add_dependency(bucket.policy)
