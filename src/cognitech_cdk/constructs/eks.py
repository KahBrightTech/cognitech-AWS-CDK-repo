"""EKS clusters, managed node groups and in-cluster access, built from YAML.

Mirrors the cluster half of the Terraform `Deploy-eks` module (the `eks`
formation variable without its `addons`, `ingress` and Helm-based `compute`
features, which live in `eks_addons`):

    eks:
      - name: main                       # cluster key used by other stacks
        version: "1.36"
        role: eks-cluster                # iam.roles name (or role_arn); optional
        subnet_tier: private             # public | private
        security_groups: [eks-cluster]   # additional control-plane groups
        endpoint_private_access: true
        endpoint_public_access: true
        public_access_cidrs: [203.0.113.0/24]
        logging: [api, audit, authenticator]
        access_entries:
          admin:
            principal_arns: [arn:aws:iam::111122223333:role/admin]
            policy_arns: [AmazonEKSClusterAdminPolicy]
        # launch_templates and node_groups are optional. Omit both for a
        # control-plane-only cluster (subnets + security groups, no workers).
        namespaces:
          - name: team-a
            resource_quota: {hard: {pods: "50"}}
        rbac:
          cluster_roles: [...]
          cluster_role_bindings: [...]
          roles: [...]
          role_bindings: [...]

Names follow `{account_name}-{region_prefix}-{vpc}-{cluster}-eks-cluster`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks_v2 as eks
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from cognitech_cdk.common.documents import json_compatible, load_document, load_text
from cognitech_cdk.common.schema import (
    boolean,
    integer,
    items,
    lookup,
    mapping,
    name,
    required_string,
    string,
    string_map,
    strings,
)
from cognitech_cdk.constructs.network import Network

DEFAULT_VERSION = "1.36"
SUBNET_TIERS = ("public", "private")
# CONFIG_MAP alone would lock out the kubectl handler, which uses an access entry.
AUTHENTICATION_MODES = ("API", "API_AND_CONFIG_MAP")
LOG_TYPES = {
    "api": "API",
    "audit": "AUDIT",
    "authenticator": "AUTHENTICATOR",
    "controllermanager": "CONTROLLER_MANAGER",
    "controller_manager": "CONTROLLER_MANAGER",
    "scheduler": "SCHEDULER",
}
AMI_TYPES = tuple(member.value for member in eks.NodegroupAmiType)
# Graviton families such as t4g, m7g and c7gn.
GRAVITON = re.compile(r"[a-z]+[0-9]+[a-z]*g[a-z]*\.")
CAPACITY_TYPES = tuple(member.value for member in eks.CapacityType)
TAINT_EFFECTS = tuple(member.value for member in eks.TaintEffect)
REMOVAL_POLICIES = {"retain": cdk.RemovalPolicy.RETAIN, "destroy": cdk.RemovalPolicy.DESTROY}
SUBJECT_KINDS = ("User", "Group", "ServiceAccount")
BUILT_IN_CLUSTER_ROLES = ("cluster-admin", "admin", "edit", "view")

VERSION = re.compile(r"1\.\d{1,2}")
ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+")
PRINCIPAL_ARN = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:(?:role|user)/[\w+=,.@/-]+")
ACCESS_POLICY_ARN = re.compile(r"arn:aws[a-z-]*:eks::aws:cluster-access-policy/[A-Za-z0-9]+")
ACCESS_POLICY_NAME = re.compile(r"AmazonEKS[A-Za-z0-9]+")
KMS_KEY_ARN = re.compile(r"arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key/[0-9a-f-]+")
LAUNCH_TEMPLATE_ID = re.compile(r"lt-[0-9a-f]{8,17}")
AMI_ID = re.compile(r"ami-[0-9a-f]{8,17}")
INSTANCE_TYPE = re.compile(r"[a-z][a-z0-9-]*\.[a-z0-9]+")
DEVICE_NAME = re.compile(r"/dev/[a-z0-9]+")
# DNS-1123 label (namespaces) and the looser names RBAC objects allow.
K8S_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
K8S_OBJECT = re.compile(r"[a-z0-9](?:[-a-z0-9.:]{0,251}[a-z0-9])?")
QUANTITY = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[KMGTPE]i?|[mkMGTPE]|e[0-9]+)?")


def construct_id(value: str) -> str:
    """`system-ng` -> `SystemNg`, so logical ids stay readable."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", value) if part)


def _enum(value: Any, label: str, allowed: tuple[str, ...], *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or value.upper() not in allowed:
        raise ValueError(f"{label} must be one of {', '.join(allowed)}.")
    return value.upper()


def _matching(value: Any, label: str, pattern: re.Pattern[str], hint: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be {hint}.")
    return value


def _cidrs(value: Any, label: str) -> tuple[str, ...]:
    result = strings(value, label)
    for cidr in result:
        try:
            network = ipaddress.ip_network(cidr)
        except ValueError:
            raise ValueError(f"{label}: '{cidr}' is not a valid CIDR.") from None
        if network.version != 4:
            raise ValueError(f"{label}: '{cidr}' must be an IPv4 CIDR.")
    return result


def _k8s_name(value: Any, label: str, pattern: re.Pattern[str] = K8S_OBJECT) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase Kubernetes name.")
    return value


def _labels(value: Any, label: str) -> dict[str, str]:
    result = string_map(value, label)
    for key, item in result.items():
        if len(key) > 316 or len(item) > 63:
            raise ValueError(f"{label}: label '{key}' is too long.")
    return result


def _references(names: tuple[str, ...], values: Mapping[str, Any], label: str) -> list[Any]:
    return [lookup(values, item, label) for item in names]


def role_reference(
    scope: Construct,
    construct_name: str,
    role: str | None,
    role_arn: str | None,
    roles: Mapping[str, iam.IRole],
    label: str,
) -> iam.IRole | None:
    """A role from the IAM stack by name, an existing role by ARN, or `None`."""
    if role is not None:
        return lookup(roles, role, label)
    if role_arn is not None:
        return iam.Role.from_role_arn(scope, construct_name, role_arn, mutable=False)
    return None


def parse_role(data: Mapping[str, Any], label: str, *, key: str = "role") -> tuple[str | None, str | None]:
    """`role` (iam.roles name) and `role_arn` are mutually exclusive."""
    role = string(data.get(key), f"{label}: {key}")
    if role is not None:
        name(role, f"{label}: {key}")
    role_arn = _matching(
        data.get(f"{key}_arn"), f"{label}: {key}_arn", ROLE_ARN, "an IAM role ARN"
    )
    if role is not None and role_arn is not None:
        raise ValueError(f"{label}: provide only one of {key} or {key}_arn.")
    return role, role_arn


# --- access ----------------------------------------------------------------


@dataclass(frozen=True)
class AccessEntryProps:
    """One `access_entries` group; each principal gets its own access entry."""

    name: str
    principal_arns: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    policy_arns: tuple[str, ...] = ()
    kubernetes_groups: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, entry_name: str, raw: Any, cluster: str) -> AccessEntryProps:
        label = f"EKS cluster '{cluster}', access entry '{entry_name}'"
        data = mapping(
            raw if raw is not None else {}, label,
            {"principal_arns", "roles", "policy_arn", "policy_arns", "kubernetes_groups", "namespaces"},
        )
        principals = strings(data.get("principal_arns"), f"{label}: principal_arns")
        for arn in principals:
            _matching(arn, f"{label}: principal_arns", PRINCIPAL_ARN, "IAM role or user ARNs")
        roles = strings(data.get("roles"), f"{label}: roles")
        for role in roles:
            name(role, f"{label}: roles")
        if not principals and not roles:
            raise ValueError(f"{label}: list at least one of principal_arns or roles.")
        policies = list(strings(data.get("policy_arns"), f"{label}: policy_arns"))
        if data.get("policy_arn") is not None:
            policies.insert(0, required_string(data["policy_arn"], f"{label}: policy_arn"))
        for policy in policies:
            if not (ACCESS_POLICY_ARN.fullmatch(policy) or ACCESS_POLICY_NAME.fullmatch(policy)):
                raise ValueError(
                    f"{label}: '{policy}' must be an EKS access policy name or "
                    "arn:aws:eks::aws:cluster-access-policy/... ARN."
                )
        if len(set(policies)) != len(policies):
            raise ValueError(f"{label}: duplicate access policy.")
        groups = strings(data.get("kubernetes_groups"), f"{label}: kubernetes_groups")
        if any(group.startswith("system:") for group in groups):
            raise ValueError(
                f"{label}: kubernetes_groups must not start with 'system:'; the EKS "
                "access entry API rejects them. Use an access policy instead."
            )
        if not policies and not groups:
            raise ValueError(f"{label}: grant at least one policy_arns or kubernetes_groups.")
        namespaces = strings(data.get("namespaces"), f"{label}: namespaces")
        for namespace in namespaces:
            _k8s_name(namespace, f"{label}: namespaces", K8S_LABEL)
        if namespaces and not policies:
            raise ValueError(f"{label}: namespaces scope access policies, so list policy_arns.")
        return cls(
            name=name(entry_name, f"EKS cluster '{cluster}', access entry name"),
            principal_arns=principals, roles=roles, policy_arns=tuple(policies),
            kubernetes_groups=groups, namespaces=namespaces,
        )


# --- compute ---------------------------------------------------------------


@dataclass(frozen=True)
class LaunchTemplateProps:
    name: str
    instance_type: str | None = None
    ami_id: str | None = None
    key_pair: str | None = None
    security_groups: tuple[str, ...] = ()
    include_cluster_security_group: bool = True
    volume_size: int = 20
    volume_type: str = "gp3"
    root_device_name: str = "/dev/xvda"
    user_data: str | None = None
    http_put_response_hop_limit: int = 2
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, *, base_dir: Path | None) -> LaunchTemplateProps:
        label = f"EKS cluster '{cluster}', launch template"
        data = mapping(raw, label, {
            "name", "instance_type", "ami_id", "key_pair", "security_groups",
            "include_cluster_security_group", "volume_size", "volume_type",
            "root_device_name", "user_data", "http_put_response_hop_limit", "tags",
        }, required=("name",))
        lt_name = name(data["name"], f"{label} name")
        label = f"EKS cluster '{cluster}', launch template '{lt_name}'"
        user_data = None
        if data.get("user_data") is not None:
            user_data = load_text(
                required_string(data["user_data"], f"{label}: user_data"),
                f"{label}: user_data", base_dir=base_dir,
            )
        groups = strings(data.get("security_groups"), f"{label}: security_groups")
        for group in groups:
            name(group, f"{label}: security_groups")
        key_pair = string(data.get("key_pair"), f"{label}: key_pair")
        if key_pair is not None:
            name(key_pair, f"{label}: key_pair")
        return cls(
            name=lt_name,
            instance_type=_matching(
                data.get("instance_type"), f"{label}: instance_type", INSTANCE_TYPE,
                "an EC2 instance type such as m6i.large",
            ),
            ami_id=_matching(data.get("ami_id"), f"{label}: ami_id", AMI_ID, "an ami- id"),
            key_pair=key_pair,
            security_groups=groups,
            include_cluster_security_group=boolean(
                data.get("include_cluster_security_group"),
                f"{label}: include_cluster_security_group", default=True,
            ),
            volume_size=integer(
                data.get("volume_size"), f"{label}: volume_size", default=20,
                minimum=4, maximum=16384,
            ),
            volume_type=_enum(
                data.get("volume_type"), f"{label}: volume_type",
                ("GP3", "GP2", "IO1", "IO2"), default="GP3",
            ).lower(),
            root_device_name=_matching(
                data.get("root_device_name"), f"{label}: root_device_name", DEVICE_NAME,
                "a device name such as /dev/xvda",
            ) or "/dev/xvda",
            user_data=user_data,
            http_put_response_hop_limit=integer(
                data.get("http_put_response_hop_limit"),
                f"{label}: http_put_response_hop_limit", default=2, minimum=1, maximum=64,
            ),
            tags=string_map(data.get("tags"), f"{label}: tags"),
        )


@dataclass(frozen=True)
class TaintProps:
    key: str
    effect: str
    value: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> TaintProps:
        data = mapping(raw, f"{label}: taint", {"key", "value", "effect"}, required=("key", "effect"))
        return cls(
            key=required_string(data["key"], f"{label}: taint key"),
            value=string(data.get("value"), f"{label}: taint value"),
            effect=_enum(data["effect"], f"{label}: taint effect", TAINT_EFFECTS),
        )


@dataclass(frozen=True)
class NodeGroupProps:
    name: str
    min_size: int
    desired_size: int
    max_size: int
    role: str | None = None
    role_arn: str | None = None
    subnet_tier: str | None = None
    instance_types: tuple[str, ...] = ()
    ami_type: str | None = None
    disk_size: int | None = None
    capacity_type: str = "ON_DEMAND"
    labels: Mapping[str, str] = field(default_factory=dict)
    taints: tuple[TaintProps, ...] = ()
    tags: Mapping[str, str] = field(default_factory=dict)
    force_update: bool = False
    version: str | None = None
    release_version: str | None = None
    ssh_key_pair: str | None = None
    ssh_source_security_groups: tuple[str, ...] = ()
    launch_template: str | None = None
    launch_template_id: str | None = None
    launch_template_version: str | None = None
    max_unavailable: int | None = None

    @property
    def uses_launch_template(self) -> bool:
        return self.launch_template is not None or self.launch_template_id is not None

    @classmethod
    def from_dict(cls, raw: Any, cluster: str) -> NodeGroupProps:
        label = f"EKS cluster '{cluster}', node group"
        data = mapping(raw, label, {
            "name", "role", "role_arn", "subnet_tier", "min_size", "desired_size",
            "max_size", "instance_types", "ami_type", "disk_size", "capacity_type",
            "labels", "taints", "tags", "force_update", "version", "release_version",
            "remote_access", "launch_template", "launch_template_id",
            "launch_template_version", "max_unavailable",
        }, required=("name", "min_size", "desired_size", "max_size"))
        group_name = name(data["name"], f"{label} name")
        label = f"EKS cluster '{cluster}', node group '{group_name}'"
        role, role_arn = parse_role(data, label)
        min_size = integer(data["min_size"], f"{label}: min_size", minimum=0)
        desired = integer(data["desired_size"], f"{label}: desired_size", minimum=0)
        max_size = integer(data["max_size"], f"{label}: max_size", minimum=1)
        if not min_size <= desired <= max_size:
            raise ValueError(f"{label}: sizes must satisfy min_size <= desired_size <= max_size.")
        instance_types = strings(data.get("instance_types"), f"{label}: instance_types")
        for instance_type in instance_types:
            _matching(instance_type, f"{label}: instance_types", INSTANCE_TYPE, "EC2 instance types")
        ami_type = _enum(data.get("ami_type"), f"{label}: ami_type", AMI_TYPES)
        if ami_type is not None and ami_type.startswith("AL2_"):
            raise ValueError(
                f"{label}: Amazon Linux 2 AMIs are not published for Kubernetes 1.33 "
                "and later; use an AL2023 or Bottlerocket ami_type."
            )
        remote = data.get("remote_access")
        ssh_key = None
        ssh_groups: tuple[str, ...] = ()
        if remote is not None:
            remote = mapping(remote, f"{label}: remote_access", {"key_pair", "source_security_groups"}, required=("key_pair",))
            ssh_key = name(remote["key_pair"], f"{label}: remote_access.key_pair")
            ssh_groups = strings(remote.get("source_security_groups"), f"{label}: remote_access.source_security_groups")
            if not ssh_groups:
                raise ValueError(
                    f"{label}: remote_access needs source_security_groups; without them "
                    "EKS opens SSH to 0.0.0.0/0."
                )
        launch_template = string(data.get("launch_template"), f"{label}: launch_template")
        launch_template_id = _matching(
            data.get("launch_template_id"), f"{label}: launch_template_id",
            LAUNCH_TEMPLATE_ID, "an lt- id",
        )
        if launch_template is not None and launch_template_id is not None:
            raise ValueError(f"{label}: provide only one of launch_template or launch_template_id.")
        lt_version = data.get("launch_template_version")
        if lt_version is not None and (isinstance(lt_version, bool) or not isinstance(lt_version, (str, int))):
            raise ValueError(f"{label}: launch_template_version must be a version number or string.")
        if lt_version is not None and launch_template_id is None:
            raise ValueError(f"{label}: launch_template_version applies to launch_template_id only.")
        props = cls(
            name=group_name, min_size=min_size, desired_size=desired, max_size=max_size,
            role=role, role_arn=role_arn,
            subnet_tier=_subnet_tier(data.get("subnet_tier"), f"{label}: subnet_tier", None),
            instance_types=instance_types, ami_type=ami_type,
            disk_size=integer(data.get("disk_size"), f"{label}: disk_size", minimum=1),
            capacity_type=_enum(data.get("capacity_type"), f"{label}: capacity_type", CAPACITY_TYPES, default="ON_DEMAND"),
            labels=_labels(data.get("labels"), f"{label}: labels"),
            taints=tuple(TaintProps.from_dict(item, label) for item in _list(data.get("taints"), f"{label}: taints")),
            tags=string_map(data.get("tags"), f"{label}: tags"),
            force_update=boolean(data.get("force_update"), f"{label}: force_update"),
            version=_version(data.get("version"), f"{label}: version", None),
            release_version=string(data.get("release_version"), f"{label}: release_version"),
            ssh_key_pair=ssh_key, ssh_source_security_groups=ssh_groups,
            launch_template=launch_template, launch_template_id=launch_template_id,
            launch_template_version=str(lt_version) if lt_version is not None else None,
            max_unavailable=integer(data.get("max_unavailable"), f"{label}: max_unavailable", minimum=1, maximum=100),
        )
        if props.uses_launch_template and props.disk_size is not None:
            raise ValueError(f"{label}: disk_size cannot be combined with a launch template; set volume_size there.")
        if props.uses_launch_template and props.ssh_key_pair is not None:
            raise ValueError(f"{label}: remote_access cannot be combined with a launch template; set key_pair there.")
        return props


def _list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return value


def _subnet_tier(value: Any, label: str, default: str | None) -> str | None:
    if value is None:
        return default
    if value not in SUBNET_TIERS:
        raise ValueError(f"{label} must be public or private.")
    return value


def _version(value: Any, label: str, default: str | None) -> str | None:
    if value is None:
        return default
    # An unquoted 1.30 would arrive as the float 1.3, so only strings are accepted.
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError(f"{label} must be a quoted Kubernetes version such as \"1.36\".")
    return value


# --- in-cluster objects ----------------------------------------------------


@dataclass(frozen=True)
class ResourceQuotaProps:
    name: str | None = None
    hard: Mapping[str, str] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NamespaceProps:
    name: str
    labels: Mapping[str, str] = field(default_factory=dict)
    resource_quota: ResourceQuotaProps | None = None

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, *, base_dir: Path | None) -> NamespaceProps:
        label = f"EKS cluster '{cluster}', namespace"
        data = mapping(raw, label, {"name", "labels", "resource_quota"}, required=("name",))
        namespace = _k8s_name(data["name"], f"{label} name", K8S_LABEL)
        label = f"EKS cluster '{cluster}', namespace '{namespace}'"
        quota = None
        if data.get("resource_quota") is not None:
            quota_data = mapping(data["resource_quota"], f"{label}: resource_quota", {"name", "hard", "scopes", "file"})
            if quota_data.get("file") is not None:
                if "hard" in quota_data or "scopes" in quota_data:
                    raise ValueError(f"{label}: resource_quota takes file or hard/scopes, not both.")
                document = mapping(
                    load_document(quota_data["file"], f"{label}: resource_quota file", base_dir=base_dir),
                    f"{label}: resource_quota file", {"apiVersion", "kind", "metadata", "spec"},
                )
                spec = document.get("spec") or {}
                if not isinstance(spec, Mapping):
                    raise ValueError(f"{label}: resource_quota file spec must be a mapping.")
                metadata = document.get("metadata") or {}
                quota_name = quota_data.get("name") or (metadata.get("name") if isinstance(metadata, Mapping) else None)
                hard, scopes = spec.get("hard"), spec.get("scopes")
            else:
                quota_name, hard, scopes = quota_data.get("name"), quota_data.get("hard"), quota_data.get("scopes")
            hard_map = string_map(hard, f"{label}: resource_quota hard")
            if not hard_map:
                raise ValueError(f"{label}: resource_quota needs at least one hard limit.")
            for key, value in hard_map.items():
                if not QUANTITY.fullmatch(value):
                    raise ValueError(f"{label}: resource_quota hard '{key}' must be a Kubernetes quantity.")
            quota = ResourceQuotaProps(
                name=_k8s_name(quota_name, f"{label}: resource_quota name") if quota_name else None,
                hard=hard_map,
                scopes=strings(scopes, f"{label}: resource_quota scopes"),
            )
        return cls(name=namespace, labels=_labels(data.get("labels"), f"{label}: labels"), resource_quota=quota)


def _rules(value: Any, label: str) -> tuple[dict[str, list[str]], ...]:
    rules = []
    for raw in _list(value, f"{label}: rules"):
        data = mapping(raw, f"{label}: rule", {"api_groups", "resources", "verbs", "resource_names"}, required=("verbs",))
        rule = {
            "apiGroups": list(strings(data.get("api_groups"), f"{label}: api_groups")) or [""],
            "resources": list(strings(data.get("resources"), f"{label}: resources", allow_empty=False)),
            "verbs": list(strings(data["verbs"], f"{label}: verbs", allow_empty=False)),
        }
        if data.get("resource_names") is not None:
            rule["resourceNames"] = list(strings(data["resource_names"], f"{label}: resource_names"))
        rules.append(rule)
    if not rules:
        raise ValueError(f"{label}: needs at least one rule.")
    return tuple(rules)


def _subjects(value: Any, label: str) -> tuple[dict[str, str], ...]:
    subjects = []
    for raw in _list(value, f"{label}: subjects"):
        data = mapping(raw, f"{label}: subject", {"kind", "name", "namespace", "api_group"}, required=("kind", "name"))
        if data["kind"] not in SUBJECT_KINDS:
            raise ValueError(f"{label}: subject kind must be one of {', '.join(SUBJECT_KINDS)}.")
        subject = {"kind": data["kind"], "name": required_string(data["name"], f"{label}: subject name")}
        if data["kind"] == "ServiceAccount":
            subject["namespace"] = _k8s_name(
                data.get("namespace"), f"{label}: ServiceAccount subjects need a namespace", K8S_LABEL
            )
            if data.get("api_group"):
                raise ValueError(f"{label}: ServiceAccount subjects take no api_group.")
        else:
            subject["apiGroup"] = string(
                data.get("api_group"), f"{label}: api_group", default="rbac.authorization.k8s.io"
            )
            if data.get("namespace") is not None:
                subject["namespace"] = _k8s_name(data["namespace"], f"{label}: subject namespace", K8S_LABEL)
        subjects.append(subject)
    if not subjects:
        raise ValueError(f"{label}: needs at least one subject.")
    return tuple(subjects)


@dataclass(frozen=True)
class RbacObjectProps:
    """A ClusterRole, ClusterRoleBinding, Role or RoleBinding."""

    kind: str
    name: str
    namespace: str | None = None
    rules: tuple[Mapping[str, Any], ...] = ()
    role_ref: str | None = None
    subjects: tuple[Mapping[str, str], ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)

    def manifest(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {"name": self.name}
        if self.namespace:
            metadata["namespace"] = self.namespace
        if self.labels:
            metadata["labels"] = dict(self.labels)
        body: dict[str, Any] = {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": self.kind, "metadata": metadata,
        }
        if self.kind in ("ClusterRole", "Role"):
            body["rules"] = [dict(rule) for rule in self.rules]
        else:
            body["roleRef"] = {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole" if self.kind == "ClusterRoleBinding" else "Role",
                "name": self.role_ref,
            }
            body["subjects"] = [dict(subject) for subject in self.subjects]
        return body


@dataclass(frozen=True)
class RbacProps:
    cluster_roles: tuple[RbacObjectProps, ...] = ()
    cluster_role_bindings: tuple[RbacObjectProps, ...] = ()
    roles: tuple[RbacObjectProps, ...] = ()
    role_bindings: tuple[RbacObjectProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.cluster_roles or self.cluster_role_bindings or self.roles or self.role_bindings)

    @property
    def objects(self) -> tuple[RbacObjectProps, ...]:
        return (*self.cluster_roles, *self.roles, *self.cluster_role_bindings, *self.role_bindings)

    @classmethod
    def from_dict(cls, raw: Any, cluster: str) -> RbacProps:
        label = f"EKS cluster '{cluster}', rbac"
        if raw is None:
            return cls()
        data = mapping(raw, label, {"cluster_roles", "cluster_role_bindings", "roles", "role_bindings"})

        def parse(kind: str, key: str, ref_key: str | None) -> tuple[RbacObjectProps, ...]:
            def one(item: Any) -> RbacObjectProps:
                allowed = {"name", "labels"}
                allowed |= {"rules"} if ref_key is None else {ref_key, "subjects"}
                if kind in ("Role", "RoleBinding"):
                    allowed.add("namespace")
                body = mapping(item, f"{label}: {key}", allowed, required=("name",))
                object_name = _k8s_name(body["name"], f"{label}: {key} name")
                where = f"{label}, {kind} '{object_name}'"
                namespace = None
                if kind in ("Role", "RoleBinding"):
                    namespace = _k8s_name(body.get("namespace") or "default", f"{where}: namespace", K8S_LABEL)
                return RbacObjectProps(
                    kind=kind, name=object_name, namespace=namespace,
                    rules=_rules(body.get("rules"), where) if ref_key is None else (),
                    role_ref=_k8s_name(body.get(ref_key), f"{where}: {ref_key}") if ref_key else None,
                    subjects=_subjects(body.get("subjects"), where) if ref_key else (),
                    labels=_labels(body.get("labels"), f"{where}: labels"),
                )

            return items(
                data.get(key), f"{label}: {key}", one,
                key=lambda item: f"{item.namespace or ''}/{item.name}",
            )

        return cls(
            cluster_roles=parse("ClusterRole", "cluster_roles", None),
            cluster_role_bindings=parse("ClusterRoleBinding", "cluster_role_bindings", "cluster_role"),
            roles=parse("Role", "roles", None),
            role_bindings=parse("RoleBinding", "role_bindings", "role"),
        )


@dataclass(frozen=True)
class ServiceAccountProps:
    name: str
    namespace: str = "default"
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, cluster: str) -> ServiceAccountProps:
        label = f"EKS cluster '{cluster}', service account"
        data = mapping(raw, label, {"name", "namespace", "labels", "annotations"}, required=("name",))
        return cls(
            name=_k8s_name(data["name"], f"{label} name"),
            namespace=_k8s_name(data.get("namespace") or "default", f"{label} namespace", K8S_LABEL),
            labels=_labels(data.get("labels"), f"{label}: labels"),
            annotations=string_map(data.get("annotations"), f"{label}: annotations"),
        )


@dataclass(frozen=True)
class PodIdentityProps:
    namespace: str
    service_account: str
    role: str | None = None
    role_arn: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, cluster: str) -> PodIdentityProps:
        label = f"EKS cluster '{cluster}', pod identity association"
        data = mapping(raw, label, {"namespace", "service_account", "role", "role_arn"}, required=("namespace", "service_account"))
        role, role_arn = parse_role(data, label)
        if role is None and role_arn is None:
            raise ValueError(f"{label}: needs role or role_arn.")
        return cls(
            namespace=_k8s_name(data["namespace"], f"{label}: namespace", K8S_LABEL),
            service_account=_k8s_name(data["service_account"], f"{label}: service_account"),
            role=role, role_arn=role_arn,
        )


# --- cluster ---------------------------------------------------------------


@dataclass(frozen=True)
class EksClusterProps:
    name: str
    version: str = DEFAULT_VERSION
    role: str | None = None
    role_arn: str | None = None
    subnet_tier: str = "private"
    security_groups: tuple[str, ...] = ()
    endpoint_private_access: bool = True
    endpoint_public_access: bool = True
    public_access_cidrs: tuple[str, ...] = ("0.0.0.0/0",)
    authentication_mode: str = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions: bool = True
    bootstrap_self_managed_addons: bool = True
    logging: tuple[str, ...] = ()
    service_ipv4_cidr: str | None = None
    secrets_encryption_key_arn: str | None = None
    kubectl: bool = True
    oidc_provider: bool = False
    removal_policy: str = "retain"
    tags: Mapping[str, str] = field(default_factory=dict)
    access_entries: tuple[AccessEntryProps, ...] = ()
    launch_templates: tuple[LaunchTemplateProps, ...] = ()
    node_groups: tuple[NodeGroupProps, ...] = ()
    namespaces: tuple[NamespaceProps, ...] = ()
    rbac: RbacProps = RbacProps()
    service_accounts: tuple[ServiceAccountProps, ...] = ()
    pod_identity_associations: tuple[PodIdentityProps, ...] = ()

    @property
    def needs_kubectl(self) -> bool:
        return bool(self.namespaces or self.rbac or self.service_accounts)

    @property
    def kubectl_in_vpc(self) -> bool:
        """The kubectl Lambda cannot reach a private-only or CIDR-restricted endpoint from outside."""
        return not self.endpoint_public_access or self.public_access_cidrs != ("0.0.0.0/0",)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EksClusterProps:
        data = mapping(raw, "EKS cluster", {
            "name", "version", "role", "role_arn", "subnet_tier", "security_groups",
            "endpoint_private_access", "endpoint_public_access", "public_access_cidrs",
            "authentication_mode", "bootstrap_cluster_creator_admin_permissions",
            "bootstrap_self_managed_addons", "logging", "service_ipv4_cidr",
            "secrets_encryption_key_arn", "kubectl", "oidc_provider", "removal_policy",
            "tags", "access_entries", "launch_templates", "node_groups", "namespaces",
            "rbac", "service_accounts", "pod_identity_associations",
        }, required=("name",))
        cluster = name(data["name"], "EKS cluster name")
        label = f"EKS cluster '{cluster}'"
        role, role_arn = parse_role(data, label)
        groups = strings(data.get("security_groups"), f"{label}: security_groups")
        for group in groups:
            name(group, f"{label}: security_groups")
        if len(set(groups)) != len(groups):
            raise ValueError(f"{label}: duplicate security group.")
        if len(groups) > 5:
            raise ValueError(f"{label}: EKS accepts at most 5 additional security groups.")
        private = boolean(data.get("endpoint_private_access"), f"{label}: endpoint_private_access", default=True)
        public = boolean(data.get("endpoint_public_access"), f"{label}: endpoint_public_access", default=True)
        if not private and not public:
            raise ValueError(f"{label}: enable endpoint_private_access, endpoint_public_access or both.")
        cidrs = _cidrs(data.get("public_access_cidrs"), f"{label}: public_access_cidrs") or ("0.0.0.0/0",)
        if data.get("public_access_cidrs") is not None and not public:
            raise ValueError(f"{label}: public_access_cidrs needs endpoint_public_access: true.")
        if not public:
            cidrs = ("0.0.0.0/0",)
        logging = []
        for log_type in strings(data.get("logging"), f"{label}: logging"):
            key = log_type.replace("-", "_").lower()
            if key not in LOG_TYPES:
                raise ValueError(f"{label}: logging must use api, audit, authenticator, controllerManager or scheduler.")
            logging.append(LOG_TYPES[key])
        if len(set(logging)) != len(logging):
            raise ValueError(f"{label}: duplicate logging type.")
        service_cidr = string(data.get("service_ipv4_cidr"), f"{label}: service_ipv4_cidr")
        if service_cidr is not None:
            try:
                block = ipaddress.ip_network(service_cidr)
            except ValueError:
                raise ValueError(f"{label}: service_ipv4_cidr is not a valid CIDR.") from None
            if block.version != 4 or not 12 <= block.prefixlen <= 24 or not block.is_private:
                raise ValueError(f"{label}: service_ipv4_cidr must be a private IPv4 /12-/24 block.")
        removal = string(data.get("removal_policy"), f"{label}: removal_policy", default="retain")
        if removal not in REMOVAL_POLICIES:
            raise ValueError(f"{label}: removal_policy must be retain or destroy.")
        raw_entries = data.get("access_entries")
        if raw_entries is not None and not isinstance(raw_entries, Mapping):
            raise ValueError(f"{label}: access_entries must map names to entries.")
        access_entries = tuple(
            AccessEntryProps.from_dict(entry_name, entry, cluster)
            for entry_name, entry in (raw_entries or {}).items()
        )
        props = cls(
            name=cluster,
            version=_version(data.get("version"), f"{label}: version", DEFAULT_VERSION),
            role=role, role_arn=role_arn,
            subnet_tier=_subnet_tier(data.get("subnet_tier"), f"{label}: subnet_tier", "private"),
            security_groups=groups,
            endpoint_private_access=private, endpoint_public_access=public,
            public_access_cidrs=cidrs,
            authentication_mode=_enum(
                data.get("authentication_mode"), f"{label}: authentication_mode",
                AUTHENTICATION_MODES, default="API_AND_CONFIG_MAP",
            ),
            bootstrap_cluster_creator_admin_permissions=boolean(
                data.get("bootstrap_cluster_creator_admin_permissions"),
                f"{label}: bootstrap_cluster_creator_admin_permissions", default=True,
            ),
            bootstrap_self_managed_addons=boolean(
                data.get("bootstrap_self_managed_addons"),
                f"{label}: bootstrap_self_managed_addons", default=True,
            ),
            logging=tuple(logging),
            service_ipv4_cidr=service_cidr,
            secrets_encryption_key_arn=_matching(
                data.get("secrets_encryption_key_arn"), f"{label}: secrets_encryption_key_arn",
                KMS_KEY_ARN, "a KMS key ARN",
            ),
            kubectl=boolean(data.get("kubectl"), f"{label}: kubectl", default=True),
            oidc_provider=boolean(data.get("oidc_provider"), f"{label}: oidc_provider"),
            removal_policy=removal,
            tags=string_map(data.get("tags"), f"{label}: tags"),
            access_entries=access_entries,
            launch_templates=items(
                data.get("launch_templates"), f"{label}: launch_templates",
                lambda item: LaunchTemplateProps.from_dict(item, cluster, base_dir=base_dir),
            ),
            node_groups=items(
                data.get("node_groups"), f"{label}: node_groups",
                lambda item: NodeGroupProps.from_dict(item, cluster),
            ),
            namespaces=items(
                data.get("namespaces"), f"{label}: namespaces",
                lambda item: NamespaceProps.from_dict(item, cluster, base_dir=base_dir),
            ),
            rbac=RbacProps.from_dict(data.get("rbac"), cluster),
            service_accounts=items(
                data.get("service_accounts"), f"{label}: service_accounts",
                lambda item: ServiceAccountProps.from_dict(item, cluster),
                key=lambda item: f"{item.namespace}/{item.name}",
            ),
            pod_identity_associations=items(
                data.get("pod_identity_associations"), f"{label}: pod_identity_associations",
                lambda item: PodIdentityProps.from_dict(item, cluster),
                key=lambda item: f"{item.namespace}/{item.service_account}",
            ),
        )
        props._validate(label)
        return props

    def _validate(self, label: str) -> None:
        principals: set[str] = set()
        for entry in self.access_entries:
            for principal in (*entry.principal_arns, *(f"role:{role}" for role in entry.roles)):
                if principal in principals:
                    raise ValueError(f"{label}: principal '{principal}' appears in two access entries.")
                principals.add(principal)
        templates = {template.name: template for template in self.launch_templates}
        for group in self.node_groups:
            where = f"{label}, node group '{group.name}'"
            if group.launch_template is None:
                continue
            template = templates.get(group.launch_template)
            if template is None:
                known = ", ".join(sorted(templates)) or "none"
                raise ValueError(f"{where}: unknown launch_template '{group.launch_template}'. Configured: {known}.")
            if template.instance_type and group.instance_types:
                raise ValueError(f"{where}: set instance types on the node group or the launch template, not both.")
            if template.ami_id and group.ami_type:
                raise ValueError(f"{where}: a custom AMI launch template cannot be combined with ami_type.")
            if template.ami_id and self.service_ipv4_cidr is None:
                raise ValueError(f"{label}: launch template '{template.name}' uses a custom AMI, so set service_ipv4_cidr for its NodeConfig.")
        if self.needs_kubectl and not self.kubectl:
            raise ValueError(f"{label}: namespaces, rbac and service_accounts need kubectl: true.")
        cluster_roles = {item.name for item in self.rbac.cluster_roles}
        for binding in self.rbac.cluster_role_bindings:
            if (
                binding.role_ref not in cluster_roles
                and binding.role_ref not in BUILT_IN_CLUSTER_ROLES
                and not binding.role_ref.startswith("system:")
            ):
                raise ValueError(
                    f"{label}: ClusterRoleBinding '{binding.name}' points at unknown cluster_role "
                    f"'{binding.role_ref}'. Configured: {', '.join(sorted(cluster_roles)) or 'none'}."
                )
        roles = {(item.namespace, item.name) for item in self.rbac.roles}
        for binding in self.rbac.role_bindings:
            if (binding.namespace, binding.role_ref) not in roles:
                raise ValueError(
                    f"{label}: RoleBinding '{binding.name}' points at role '{binding.role_ref}', "
                    f"which is not configured in namespace '{binding.namespace}'."
                )


@dataclass(frozen=True)
class EksProps:
    clusters: tuple[EksClusterProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.clusters)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EksProps:
        return cls(
            clusters=items(raw, "eks", lambda item: EksClusterProps.from_dict(item, base_dir=base_dir))
        )


# --- construct ---------------------------------------------------------------


class EksCluster(Construct):
    """One cluster, its access entries, launch templates, node groups and in-cluster objects."""

    def __init__(
        self,
        scope: Construct,
        construct_name: str,
        *,
        props: EksClusterProps,
        cluster_name: str,
        names: Callable[..., str],
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        roles: Mapping[str, iam.IRole],
        key_pairs: Mapping[str, ec2.IKeyPair],
        kubectl_layer: lambda_.ILayerVersion | None,
        access_scope: Callable[[], Construct] | None = None,
        nodes_scope: Callable[[], Construct] | None = None,
    ) -> None:
        super().__init__(scope, construct_name)
        # Access/in-cluster objects and compute can live in nested stacks; the
        # callables create them only when something is placed there.
        self._access_scope = access_scope or (lambda: self)
        self._nodes_scope = nodes_scope or (lambda: self)
        self.props = props
        self.names = names
        self.network = network
        label = f"EKS cluster '{props.name}'"

        subnets = self._subnets(props.subnet_tier, label)
        groups = _references(props.security_groups, security_groups, f"{label}: security_groups")
        kubectl_options = None
        if props.kubectl:
            if kubectl_layer is None:
                raise ValueError(f"{label}: kubectl is enabled but no kubectl layer was provided.")
            private_subnets = None
            if props.kubectl_in_vpc:
                if not props.endpoint_private_access or not network.private_subnets:
                    raise ValueError(
                        f"{label}: the API endpoint is private or CIDR-restricted, so the kubectl "
                        "handler must run in private subnets; enable endpoint_private_access and "
                        "private subnets in the network."
                    )
                private_subnets = list(network.private_subnets)
            kubectl_options = eks.KubectlProviderOptions(
                kubectl_layer=kubectl_layer, private_subnets=private_subnets
            )

        self.cluster = eks.Cluster(
            self,
            "Cluster",
            cluster_name=cluster_name,
            version=eks.KubernetesVersion.of(props.version),
            vpc=network.vpc,
            vpc_subnets=[ec2.SubnetSelection(subnets=subnets)],
            role=role_reference(self, "ClusterRole", props.role, props.role_arn, roles, f"{label}: role"),
            security_group=groups[0] if groups else None,
            endpoint_access=self._endpoint_access(),
            cluster_logging=[eks.ClusterLoggingTypes[log_type] for log_type in props.logging] or None,
            service_ipv4_cidr=props.service_ipv4_cidr,
            secrets_encryption_key=(
                kms.Key.from_key_arn(self, "SecretsKey", props.secrets_encryption_key_arn)
                if props.secrets_encryption_key_arn else None
            ),
            kubectl_provider_options=kubectl_options,
            bootstrap_cluster_creator_admin_permissions=props.bootstrap_cluster_creator_admin_permissions,
            bootstrap_self_managed_addons=props.bootstrap_self_managed_addons,
            default_capacity=0,
            default_capacity_type=eks.DefaultCapacityType.NODEGROUP,
            output_config_command=False,
            tags={**props.tags, "Name": cluster_name},
        )
        resource = self.cluster.node.default_child
        resource.add_property_override("AccessConfig.AuthenticationMode", props.authentication_mode)
        if len(groups) > 1:
            resource.add_property_override(
                "ResourcesVpcConfig.SecurityGroupIds", [group.security_group_id for group in groups]
            )
        resource.apply_removal_policy(REMOVAL_POLICIES[props.removal_policy])

        self.oidc_provider = self.cluster.open_id_connect_provider if props.oidc_provider else None
        self.access_entries = self._add_access_entries(roles)
        self.launch_templates = {
            template.name: self._add_launch_template(template, security_groups, key_pairs)
            for template in props.launch_templates
        }
        self.node_groups = {
            group.name: self._add_node_group(group, roles, security_groups, key_pairs)
            for group in props.node_groups
        }
        self.manifests = self._add_manifests()
        self.pod_identity_associations = [
            eks.CfnPodIdentityAssociation(
                self._access_scope(),
                f"PodIdentity{construct_id(item.namespace)}{construct_id(item.service_account)}",
                cluster_name=self.cluster.cluster_name,
                namespace=item.namespace,
                service_account=item.service_account,
                role_arn=role_reference(
                    self._access_scope(), f"PodIdentityRole{construct_id(item.namespace)}{construct_id(item.service_account)}",
                    item.role, item.role_arn, roles, f"{label}: pod_identity_associations",
                ).role_arn,
            )
            for item in props.pod_identity_associations
        ]

    # --- cluster ---------------------------------------------------------

    def _subnets(self, tier: str, label: str) -> list[ec2.ISubnet]:
        subnets = list(self.network.public_subnets if tier == "public" else self.network.private_subnets)
        if not subnets:
            raise ValueError(f"{label}: the network has no {tier} subnets.")
        return subnets

    def _endpoint_access(self) -> eks.EndpointAccess:
        props = self.props
        if not props.endpoint_public_access:
            return eks.EndpointAccess.PRIVATE
        access = eks.EndpointAccess.PUBLIC_AND_PRIVATE if props.endpoint_private_access else eks.EndpointAccess.PUBLIC
        if props.public_access_cidrs != ("0.0.0.0/0",):
            access = access.only_from(*props.public_access_cidrs)
        return access

    def _add_access_entries(self, roles: Mapping[str, iam.IRole]) -> dict[str, list[eks.CfnAccessEntry]]:
        entries: dict[str, list[eks.CfnAccessEntry]] = {}
        for entry in self.props.access_entries:
            principals = [
                *entry.principal_arns,
                *(role.role_arn for role in _references(
                    entry.roles, roles, f"EKS cluster '{self.props.name}', access entry '{entry.name}': roles"
                )),
            ]
            scope = (
                eks.CfnAccessEntry.AccessScopeProperty(type="namespace", namespaces=list(entry.namespaces))
                if entry.namespaces else eks.CfnAccessEntry.AccessScopeProperty(type="cluster")
            )
            policies = [
                eks.CfnAccessEntry.AccessPolicyProperty(
                    policy_arn=(
                        policy if policy.startswith("arn:")
                        else f"arn:{cdk.Aws.PARTITION}:eks::aws:cluster-access-policy/{policy}"
                    ),
                    access_scope=scope,
                )
                for policy in entry.policy_arns
            ]
            entries[entry.name] = [
                eks.CfnAccessEntry(
                    self._access_scope(),
                    f"AccessEntry{construct_id(entry.name)}{index + 1}",
                    cluster_name=self.cluster.cluster_name,
                    principal_arn=principal,
                    type="STANDARD",
                    kubernetes_groups=list(entry.kubernetes_groups) or None,
                    access_policies=policies or None,
                )
                for index, principal in enumerate(principals)
            ]
        return entries

    # --- compute ---------------------------------------------------------

    def _add_launch_template(
        self,
        template: LaunchTemplateProps,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        key_pairs: Mapping[str, ec2.IKeyPair],
    ) -> ec2.CfnLaunchTemplate:
        label = f"EKS cluster '{self.props.name}', launch template '{template.name}'"
        template_name = self.names(self.props.name, template.name, "lt")
        group_ids = [group.security_group_id for group in _references(
            template.security_groups, security_groups, f"{label}: security_groups"
        )]
        if group_ids and template.include_cluster_security_group:
            group_ids.insert(0, self.cluster.cluster_security_group_id)
        tags = [cdk.CfnTag(key=key, value=value) for key, value in {**template.tags, "Name": template_name}.items()]
        user_data = self._user_data(template)
        data = ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
            image_id=template.ami_id,
            instance_type=template.instance_type,
            key_name=(
                lookup(key_pairs, template.key_pair, f"{label}: key_pair").key_pair_name
                if template.key_pair else None
            ),
            security_group_ids=group_ids or None,
            block_device_mappings=[
                ec2.CfnLaunchTemplate.BlockDeviceMappingProperty(
                    device_name=template.root_device_name,
                    ebs=ec2.CfnLaunchTemplate.EbsProperty(
                        volume_size=template.volume_size,
                        volume_type=template.volume_type,
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                )
            ],
            metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                http_endpoint="enabled",
                http_tokens="required",
                http_put_response_hop_limit=template.http_put_response_hop_limit,
            ),
            user_data=cdk.Fn.base64(user_data) if user_data else None,
            tag_specifications=[
                ec2.CfnLaunchTemplate.TagSpecificationProperty(resource_type=kind, tags=tags)
                for kind in ("instance", "volume")
            ],
        )
        return ec2.CfnLaunchTemplate(
            self._nodes_scope(),
            f"LaunchTemplate{construct_id(template.name)}",
            launch_template_name=template_name,
            launch_template_data=data,
        )

    def _user_data(self, template: LaunchTemplateProps) -> str | None:
        """MIME multi-part user data, as managed node groups require."""
        parts: list[tuple[str, str]] = []
        if template.ami_id:
            # A custom AMI gets no bootstrap from EKS, so tell nodeadm where the cluster is.
            parts.append((
                "application/node.eks.aws",
                "\n".join([
                    "apiVersion: node.eks.aws/v1alpha1",
                    "kind: NodeConfig",
                    "spec:",
                    "  cluster:",
                    f"    name: {self.cluster.cluster_name}",
                    f"    apiServerEndpoint: {self.cluster.cluster_endpoint}",
                    f"    certificateAuthority: {self.cluster.cluster_certificate_authority_data}",
                    f"    cidr: {self.props.service_ipv4_cidr}",
                ]),
            ))
        if template.user_data:
            if template.user_data.lstrip().startswith("MIME-Version"):
                if parts:
                    raise ValueError(
                        f"EKS cluster '{self.props.name}', launch template '{template.name}': "
                        "a MIME user_data file cannot be combined with a custom AMI NodeConfig."
                    )
                return template.user_data
            content_type = (
                "application/node.eks.aws" if "kind: NodeConfig" in template.user_data
                else 'text/x-shellscript; charset="us-ascii"'
            )
            parts.append((content_type, template.user_data.rstrip("\n")))
        if not parts:
            return None
        body = ['MIME-Version: 1.0', 'Content-Type: multipart/mixed; boundary="//"', ""]
        for content_type, content in parts:
            body += ["--//", f"Content-Type: {content_type}", "", content]
        body += ["--//--", ""]
        return "\n".join(body)

    def _ami_type(self, group: NodeGroupProps) -> eks.NodegroupAmiType | None:
        """AL2023 by default: CDK otherwise picks AL2, which EKS 1.33+ does not support."""
        if group.ami_type is not None:
            return eks.NodegroupAmiType(group.ami_type)
        if group.launch_template_id is not None:
            # An external template may carry its own AMI, which rules out ami_type.
            return None
        if group.launch_template is not None and any(
            template.name == group.launch_template and template.ami_id
            for template in self.props.launch_templates
        ):
            return None
        arm = any(GRAVITON.match(item) for item in group.instance_types)
        return (
            eks.NodegroupAmiType.AL2023_ARM_64_STANDARD if arm
            else eks.NodegroupAmiType.AL2023_X86_64_STANDARD
        )

    def _add_node_group(
        self,
        group: NodeGroupProps,
        roles: Mapping[str, iam.IRole],
        security_groups: Mapping[str, ec2.ISecurityGroup],
        key_pairs: Mapping[str, ec2.IKeyPair],
    ) -> eks.Nodegroup:
        label = f"EKS cluster '{self.props.name}', node group '{group.name}'"
        nodegroup_name = self.names(self.props.name, group.name, "ng")
        if len(nodegroup_name) > 63:
            raise ValueError(f"{label}: node group name '{nodegroup_name}' is longer than 63 characters.")
        launch_template = None
        if group.launch_template is not None:
            template = self.launch_templates[group.launch_template]
            launch_template = eks.LaunchTemplateSpec(id=template.ref, version=template.attr_latest_version_number)
        elif group.launch_template_id is not None:
            launch_template = eks.LaunchTemplateSpec(
                id=group.launch_template_id, version=group.launch_template_version
            )
        remote_access = None
        if group.ssh_key_pair is not None:
            remote_access = eks.NodegroupRemoteAccess(
                ssh_key_name=lookup(key_pairs, group.ssh_key_pair, f"{label}: remote_access.key_pair").key_pair_name,
                source_security_groups=_references(
                    group.ssh_source_security_groups, security_groups,
                    f"{label}: remote_access.source_security_groups",
                ),
            )
        node_group = eks.Nodegroup(
            self._nodes_scope(),
            f"NodeGroup{construct_id(group.name)}",
            cluster=self.cluster,
            nodegroup_name=nodegroup_name,
            node_role=role_reference(self._nodes_scope(), f"NodeRole{construct_id(group.name)}", group.role, group.role_arn, roles, f"{label}: role"),
            subnets=ec2.SubnetSelection(subnets=self._subnets(group.subnet_tier or self.props.subnet_tier, label)),
            min_size=group.min_size,
            desired_size=group.desired_size,
            max_size=group.max_size,
            instance_types=[ec2.InstanceType(item) for item in group.instance_types] or None,
            ami_type=self._ami_type(group),
            disk_size=group.disk_size,
            capacity_type=eks.CapacityType[group.capacity_type],
            labels=dict(group.labels) or None,
            taints=[
                eks.TaintSpec(key=taint.key, value=taint.value, effect=eks.TaintEffect[taint.effect])
                for taint in group.taints
            ] or None,
            tags={**group.tags, "Name": nodegroup_name},
            force_update=group.force_update,
            release_version=group.release_version,
            remote_access=remote_access,
            launch_template_spec=launch_template,
            max_unavailable=group.max_unavailable,
        )
        if group.version is not None:
            node_group.node.default_child.add_property_override("Version", group.version)
        return node_group

    # --- in-cluster objects ------------------------------------------------

    def _manifest(self, logical: str, documents: list[dict[str, Any]]) -> eks.KubernetesManifest:
        return eks.KubernetesManifest(
            self._access_scope(), logical, cluster=self.cluster,
            manifest=[json_compatible(document, f"EKS cluster '{self.props.name}' manifest") for document in documents],
            overwrite=True,
        )

    def _add_manifests(self) -> dict[str, eks.KubernetesManifest]:
        props = self.props
        manifests: dict[str, eks.KubernetesManifest] = {}
        if props.namespaces:
            namespaces = []
            for namespace in props.namespaces:
                metadata: dict[str, Any] = {"name": namespace.name}
                if namespace.labels:
                    metadata["labels"] = dict(namespace.labels)
                namespaces.append({"apiVersion": "v1", "kind": "Namespace", "metadata": metadata})
            manifests["namespaces"] = self._manifest("Namespaces", namespaces)
            quotas = []
            for namespace in props.namespaces:
                quota = namespace.resource_quota
                if quota is None:
                    continue
                spec: dict[str, Any] = {"hard": dict(quota.hard)}
                if quota.scopes:
                    spec["scopes"] = list(quota.scopes)
                metadata = {"name": quota.name or f"{namespace.name}-quota", "namespace": namespace.name}
                if namespace.labels:
                    metadata["labels"] = dict(namespace.labels)
                quotas.append({"apiVersion": "v1", "kind": "ResourceQuota", "metadata": metadata, "spec": spec})
            if quotas:
                manifests["resource_quotas"] = self._manifest("ResourceQuotas", quotas)
                manifests["resource_quotas"].node.add_dependency(manifests["namespaces"])
        if props.service_accounts:
            accounts = []
            for account in props.service_accounts:
                metadata = {"name": account.name, "namespace": account.namespace}
                if account.labels:
                    metadata["labels"] = dict(account.labels)
                if account.annotations:
                    metadata["annotations"] = dict(account.annotations)
                accounts.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata})
            manifests["service_accounts"] = self._manifest("ServiceAccounts", accounts)
        if props.rbac:
            manifests["rbac"] = self._manifest("Rbac", [item.manifest() for item in props.rbac.objects])
        for key in ("service_accounts", "rbac"):
            if key in manifests and "namespaces" in manifests:
                manifests[key].node.add_dependency(manifests["namespaces"])
        return manifests
