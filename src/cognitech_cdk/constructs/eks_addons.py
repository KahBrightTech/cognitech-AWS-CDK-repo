"""EKS add-ons, deployed separately from the clusters they extend.

    eks_addons:
      - cluster: main                     # a name from `eks`
        addons:
          - name: vpc-cni
          - name: coredns
          - name: kube-proxy
          - name: eks-pod-identity-agent
          - name: aws-ebs-csi-driver       # gets a Pod Identity role by default
          - name: metrics-server
          - name: external-dns
            hosted_zone_ids: [Z0123456789]  # scope Route53 writes; default all zones
          - name: cert-manager
            pod_identity: true             # Route53 DNS-01 permissions
          - name: amazon-cloudwatch-observability
            version: v6.7.0-eksbuild.1     # optional; EKS picks its default otherwise
            configuration_values: {containerLogs: {enabled: true}}
        aws_load_balancer_controller:
          enabled: true
          chart_version: 3.6.0
        helm_charts:                       # any other Helm-based Terraform add-on
          - name: ingress-nginx
            chart: ingress-nginx
            repository: https://kubernetes.github.io/ingress-nginx
            version: 4.11.2
            namespace: ingress-nginx
            values: {controller: {replicaCount: 2}}

Native add-ons are `AWS::EKS::Addon`. The AWS Load Balancer Controller is not
a native add-on for Kubernetes 1.36, so it is installed with Helm through the
cluster's kubectl provider. Add-ons that call AWS use EKS Pod Identity roles,
which requires `eks-pod-identity-agent` in the same list.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from cognitech_cdk.common import schema
from cognitech_cdk.common.documents import json_compatible, load_document, load_text, repository_path

POD_IDENTITY_AGENT = "eks-pod-identity-agent"
RESOLVE_CONFLICTS = ("OVERWRITE", "NONE", "PRESERVE")
VERSION = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*")
HOSTED_ZONE = re.compile(r"Z[A-Z0-9]{1,31}")
K8S_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
HELM_REPOSITORY = re.compile(r"(https://|oci://)[^\s]+")
DEFAULT_LBC_POLICY = "policies/aws-load-balancer-controller-v3.6.0.json"
DEFAULT_LBC_CHART = "3.6.0"
DEFAULT_KARPENTER_POLICY = "policies/karpenter-controller-v1.15.0.json"
DEFAULT_GATEWAY_CRDS = "charts/gateway-api-crds"
DEFAULT_SSL_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
SCHEMES = ("internet-facing", "internal")
SUBNET_TIERS = ("public", "private")
CAPACITY_TYPES = ("on-demand", "spot")
ARCHITECTURES = ("amd64", "arm64")
SSL_POLICY = re.compile(r"ELBSecurityPolicy-[A-Za-z0-9-]+")
CERT_ARN = re.compile(r"arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[A-Za-z0-9-]+")
DURATION = re.compile(r"[0-9]+(s|m|h)|Never")


@dataclass(frozen=True)
class AddonSpec:
    """What a native add-on needs from AWS when Pod Identity is enabled."""

    namespace: str | None = None
    service_account: str | None = None
    managed_policies: tuple[str, ...] = ()
    route53: str | None = None  # "external-dns" or "cert-manager"
    pod_identity_default: bool = False

    @property
    def supports_pod_identity(self) -> bool:
        return self.service_account is not None


NATIVE_ADDONS: Mapping[str, AddonSpec] = {
    "vpc-cni": AddonSpec("kube-system", "aws-node", ("AmazonEKS_CNI_Policy",)),
    "coredns": AddonSpec(),
    "kube-proxy": AddonSpec(),
    POD_IDENTITY_AGENT: AddonSpec(),
    "aws-ebs-csi-driver": AddonSpec(
        "kube-system", "ebs-csi-controller-sa",
        ("service-role/AmazonEBSCSIDriverPolicy",), pod_identity_default=True,
    ),
    "metrics-server": AddonSpec(),
    "external-dns": AddonSpec(
        "external-dns", "external-dns", route53="external-dns", pod_identity_default=True
    ),
    "cert-manager": AddonSpec("cert-manager", "cert-manager", route53="cert-manager"),
    "amazon-cloudwatch-observability": AddonSpec(
        "amazon-cloudwatch", "cloudwatch-agent",
        ("CloudWatchAgentServerPolicy", "AWSXrayWriteOnlyAccess"), pod_identity_default=True,
    ),
    "aws-efs-csi-driver": AddonSpec(
        "kube-system", "efs-csi-controller-sa",
        ("service-role/AmazonEFSCSIDriverPolicy",), pod_identity_default=True,
    ),
    "aws-fsx-csi-driver": AddonSpec(
        "kube-system", "fsx-csi-controller-sa", ("AmazonFSxFullAccess",), pod_identity_default=True,
    ),
    # Installs the Secrets Store CSI driver and the AWS provider; workloads use their own identity.
    "aws-secrets-store-csi-driver-provider": AddonSpec(),
}
# Installed before Karpenter so its controller has networking, DNS and credentials.
CORE_ADDONS = ("vpc-cni", "kube-proxy", "coredns", POD_IDENTITY_AGENT)


def _version(value: Any, label: str) -> str | None:
    result = schema.string(value, label)
    if result is not None and not VERSION.fullmatch(result):
        raise ValueError(f"{label} must be a version such as v1.2.3-eksbuild.1.")
    return result


def _values(value: Any, label: str, *, base_dir: Path | None) -> dict[str, Any]:
    if value is None:
        return {}
    document = load_document(value, label, base_dir=base_dir)
    if not isinstance(document, Mapping):
        raise ValueError(f"{label} must be a mapping or a repository-relative YAML/JSON file.")
    return json_compatible(document, label)


def _k8s_name(value: Any, label: str, default: str | None = None) -> str:
    result = schema.string(value, label, default=default)
    if result is None or not K8S_NAME.fullmatch(result):
        raise ValueError(f"{label} must be a lowercase Kubernetes name.")
    return result


@dataclass(frozen=True)
class NativeAddonProps:
    name: str
    version: str | None = None
    configuration_values: Mapping[str, Any] = field(default_factory=dict)
    resolve_conflicts: str = "OVERWRITE"
    preserve_on_delete: bool = False
    pod_identity: bool = False
    role: str | None = None
    hosted_zone_ids: tuple[str, ...] = ()

    @property
    def spec(self) -> AddonSpec:
        return NATIVE_ADDONS[self.name]

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, *, base_dir: Path | None) -> NativeAddonProps:
        data = schema.mapping(raw, f"EKS add-ons '{cluster}' addon", {
            "name", "version", "configuration_values", "resolve_conflicts",
            "preserve_on_delete", "pod_identity", "role", "hosted_zone_ids",
        }, required=("name",))
        name = data["name"]
        if name not in NATIVE_ADDONS:
            raise ValueError(
                f"EKS add-ons '{cluster}': '{name}' is not a supported native add-on. Use one of "
                f"{', '.join(sorted(NATIVE_ADDONS))}; install anything else with helm_charts."
            )
        label = f"EKS add-ons '{cluster}', addon '{name}'"
        spec = NATIVE_ADDONS[name]
        role = data.get("role")
        if role is not None:
            schema.name(role, f"{label}: role")
        pod_identity = schema.boolean(
            data.get("pod_identity"), f"{label}: pod_identity",
            default=spec.pod_identity_default or role is not None,
        )
        if (pod_identity or role is not None) and not spec.supports_pod_identity:
            raise ValueError(f"{label}: this add-on does not use AWS credentials.")
        if role is not None and not pod_identity:
            raise ValueError(f"{label}: role requires pod_identity: true.")
        zones = schema.strings(data.get("hosted_zone_ids"), f"{label}: hosted_zone_ids")
        if zones and spec.route53 is None:
            raise ValueError(f"{label}: hosted_zone_ids only apply to Route53 add-ons.")
        if zones and role is not None:
            raise ValueError(f"{label}: hosted_zone_ids scope a created role; remove them with role.")
        for zone in zones:
            if not HOSTED_ZONE.fullmatch(zone):
                raise ValueError(f"{label}: '{zone}' is not a Route53 hosted zone ID.")
        conflicts = schema.string(data.get("resolve_conflicts"), f"{label}: resolve_conflicts", default="OVERWRITE")
        if conflicts not in RESOLVE_CONFLICTS:
            raise ValueError(f"{label}: resolve_conflicts must be one of {', '.join(RESOLVE_CONFLICTS)}.")
        return cls(
            name=name,
            version=_version(data.get("version"), f"{label}: version"),
            configuration_values=_values(
                data.get("configuration_values"), f"{label}: configuration_values", base_dir=base_dir
            ),
            resolve_conflicts=conflicts,
            preserve_on_delete=schema.boolean(data.get("preserve_on_delete"), f"{label}: preserve_on_delete"),
            pod_identity=pod_identity,
            role=role,
            hosted_zone_ids=zones,
        )


@dataclass(frozen=True)
class LoadBalancerControllerProps:
    enabled: bool = False
    chart_version: str = DEFAULT_LBC_CHART
    namespace: str = "kube-system"
    service_account: str = "aws-load-balancer-controller"
    role: str | None = None
    policy: Mapping[str, Any] | None = None
    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, *, base_dir: Path | None) -> LoadBalancerControllerProps:
        if raw is None:
            return cls()
        label = f"EKS add-ons '{cluster}', aws_load_balancer_controller"
        data = schema.mapping(raw, label, {
            "enabled", "chart_version", "namespace", "service_account", "role", "policy", "values",
        })
        enabled = schema.boolean(data.get("enabled"), f"{label}: enabled", default=True)
        role = data.get("role")
        if role is not None:
            schema.name(role, f"{label}: role")
            if "policy" in data:
                raise ValueError(f"{label}: policy is attached to a created role; remove it with role.")
        policy = None
        if enabled and role is None:
            document = load_document(
                data.get("policy", DEFAULT_LBC_POLICY), f"{label}: policy", base_dir=base_dir
            )
            if not isinstance(document, Mapping) or not document.get("Statement"):
                raise ValueError(f"{label}: policy must be an IAM policy document.")
            policy = json_compatible(document, f"{label}: policy")
        return cls(
            enabled=enabled,
            chart_version=_version(data.get("chart_version"), f"{label}: chart_version") or DEFAULT_LBC_CHART,
            namespace=_k8s_name(data.get("namespace"), f"{label}: namespace", "kube-system"),
            service_account=_k8s_name(
                data.get("service_account"), f"{label}: service_account", "aws-load-balancer-controller"
            ),
            role=role,
            policy=policy,
            values=_values(data.get("values"), f"{label}: values", base_dir=base_dir),
        )


@dataclass(frozen=True)
class HelmChartProps:
    name: str
    chart: str
    repository: str
    version: str
    namespace: str
    release: str
    create_namespace: bool = True
    values: Mapping[str, Any] = field(default_factory=dict)
    wait: bool = False
    timeout_minutes: int = 15

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, *, base_dir: Path | None) -> HelmChartProps:
        data = schema.mapping(raw, f"EKS add-ons '{cluster}' helm chart", {
            "name", "chart", "repository", "version", "namespace", "release",
            "create_namespace", "values", "wait", "timeout_minutes",
        }, required=("name", "chart", "repository", "version"))
        name = _k8s_name(data["name"], f"EKS add-ons '{cluster}' helm chart name")
        label = f"EKS add-ons '{cluster}', helm chart '{name}'"
        repository = schema.required_string(data["repository"], f"{label}: repository")
        if not HELM_REPOSITORY.fullmatch(repository):
            raise ValueError(f"{label}: repository must be an https:// or oci:// URL.")
        return cls(
            name=name,
            chart=schema.required_string(data["chart"], f"{label}: chart"),
            repository=repository,
            version=_version(data["version"], f"{label}: version") or "",
            namespace=_k8s_name(data.get("namespace"), f"{label}: namespace", name),
            release=_k8s_name(data.get("release"), f"{label}: release", name),
            create_namespace=schema.boolean(data.get("create_namespace"), f"{label}: create_namespace", default=True),
            values=_values(data.get("values"), f"{label}: values", base_dir=base_dir),
            wait=schema.boolean(data.get("wait"), f"{label}: wait"),
            timeout_minutes=schema.integer(
                data.get("timeout_minutes"), f"{label}: timeout_minutes", default=15, minimum=1, maximum=60
            ) or 15,
        )


def _choice(value: Any, label: str, choices: tuple[str, ...], default: str) -> str:
    result = schema.string(value, label, default=default) or default
    if result not in choices:
        raise ValueError(f"{label} must be one of {', '.join(choices)}.")
    return result


def _host(value: Any, label: str) -> str:
    result = schema.required_string(value, label)
    if not HOST.fullmatch(result):
        raise ValueError(f"{label} must be a lowercase DNS name such as argocd.dev.example.com.")
    return result


def _certificate(data: Mapping[str, Any], label: str) -> tuple[str | None, str | None]:
    certificate = data.get("certificate")
    arn = data.get("certificate_arn")
    if certificate is not None and arn is not None:
        raise ValueError(f"{label}: provide only one of certificate or certificate_arn.")
    if certificate is not None:
        schema.name(certificate, f"{label}: certificate")
    if arn is not None and (not isinstance(arn, str) or not CERT_ARN.fullmatch(arn)):
        raise ValueError(f"{label}: certificate_arn must be an ACM certificate ARN.")
    return certificate, arn


@dataclass(frozen=True)
class LoadBalancerProps:
    """How the AWS Load Balancer Controller exposes an add-on (ALB ingress or NLB service)."""

    scheme: str = "internet-facing"
    subnet_tier: str = "public"
    security_groups: tuple[str, ...] = ()
    certificate: str | None = None
    certificate_arn: str | None = None
    ssl_policy: str = DEFAULT_SSL_POLICY
    group_name: str | None = None

    @property
    def tls(self) -> bool:
        return self.certificate is not None or self.certificate_arn is not None

    @classmethod
    def from_dict(cls, raw: Any, label: str, defaults: Mapping[str, Any] | None = None) -> LoadBalancerProps:
        data = {**(defaults or {}), **(raw or {})}
        data = schema.mapping(data, label, {
            "scheme", "subnet_tier", "security_groups", "certificate", "certificate_arn",
            "ssl_policy", "group_name",
        })
        certificate, arn = _certificate(data, label)
        ssl_policy = schema.string(data.get("ssl_policy"), f"{label}: ssl_policy", default=DEFAULT_SSL_POLICY)
        if not SSL_POLICY.fullmatch(ssl_policy or ""):
            raise ValueError(f"{label}: ssl_policy must be an ELBSecurityPolicy name.")
        group = data.get("group_name")
        if group is not None:
            group = _k8s_name(group, f"{label}: group_name")
        return cls(
            scheme=_choice(data.get("scheme"), f"{label}: scheme", SCHEMES, "internet-facing"),
            subnet_tier=_choice(data.get("subnet_tier"), f"{label}: subnet_tier", SUBNET_TIERS, "public"),
            security_groups=tuple(
                schema.name(item, f"{label}: security_groups")
                for item in schema.strings(data.get("security_groups"), f"{label}: security_groups")
            ),
            certificate=certificate,
            certificate_arn=arn,
            ssl_policy=ssl_policy or DEFAULT_SSL_POLICY,
            group_name=group,
        )


@dataclass(frozen=True)
class ChartProps:
    """Common Helm release settings for typed add-ons."""

    enabled: bool
    chart_version: str
    namespace: str
    release: str
    values: Mapping[str, Any] = field(default_factory=dict)

    KEYS = ("enabled", "chart_version", "namespace", "values")

    @classmethod
    def parse(
        cls, data: Mapping[str, Any], label: str, *, version: str, namespace: str, release: str,
        base_dir: Path | None,
    ) -> dict[str, Any]:
        return {
            "release": release,
            "enabled": schema.boolean(data.get("enabled"), f"{label}: enabled", default=True),
            "chart_version": _version(data.get("chart_version"), f"{label}: chart_version") or version,
            "namespace": _k8s_name(data.get("namespace"), f"{label}: namespace", namespace),
            "values": _values(data.get("values"), f"{label}: values", base_dir=base_dir),
        }


@dataclass(frozen=True)
class SecretsStoreCsiProps(ChartProps):
    @classmethod
    def from_dict(cls, raw: Any, cluster: str, *, base_dir: Path | None) -> SecretsStoreCsiProps | None:
        if raw is None:
            return None
        label = f"EKS add-ons '{cluster}', secrets_store_csi"
        data = schema.mapping(raw, label, cls.KEYS)
        props = cls(**cls.parse(
            data, label, version="3.1.4", namespace="kube-system",
            release="secrets-store-csi-driver-provider-aws", base_dir=base_dir,
        ))
        driver = props.values.get("secrets-store-csi-driver", {})
        if not isinstance(driver, Mapping):
            raise ValueError(f"{label}: values.secrets-store-csi-driver must be a mapping.")
        if props.enabled and driver.get("install") is False:
            raise ValueError(f"{label}: the bundled secrets-store-csi-driver must stay enabled.")
        return props


@dataclass(frozen=True)
class SecretProviderClassProps:
    name: str
    namespace: str
    parameters: Mapping[str, str]

    @classmethod
    def from_dict(cls, raw: Any, cluster: str) -> SecretProviderClassProps:
        label = f"EKS add-ons '{cluster}', secret_provider_classes"
        data = schema.mapping(raw, label, {"name", "namespace", "parameters"}, required=("name", "parameters"))
        parameters = schema.string_map(data["parameters"], f"{label}: parameters")
        schema.required_string(parameters.get("objects"), f"{label}: parameters.objects")
        if "usePodIdentity" in parameters and parameters["usePodIdentity"].lower() not in ("true", "false"):
            raise ValueError(f"{label}: usePodIdentity must be true or false.")
        return cls(
            name=_k8s_name(data["name"], f"{label}: name"),
            namespace=_k8s_name(data.get("namespace"), f"{label}: namespace", "default"),
            parameters=parameters,
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "apiVersion": "secrets-store.csi.x-k8s.io/v1",
            "kind": "SecretProviderClass",
            "metadata": {"name": self.name, "namespace": self.namespace},
            "spec": {"provider": "aws", "parameters": dict(self.parameters)},
        }


@dataclass(frozen=True)
class ArgoCdSsoProps:
    enabled: bool = False
    sso_url: str | None = None
    sso_issuer: str | None = None
    ca_data: str | None = None
    connector_name: str = "AWS IAM Identity Center"
    username_attr: str = "email"
    email_attr: str = "email"
    groups_attr: str = "groups"
    name_id_policy_format: str = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
    rbac_default_policy: str = "role:readonly"
    rbac_policies: str = ""

    @classmethod
    def from_dict(cls, raw: Any, label: str, *, base_dir: Path | None) -> ArgoCdSsoProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, label, {
            "enabled", "sso_url", "sso_issuer", "ca_pem_file", "connector_name", "username_attr",
            "email_attr", "groups_attr", "name_id_policy_format", "rbac_default_policy",
            "rbac_policies_file",
        })
        enabled = schema.boolean(data.get("enabled"), f"{label}: enabled")
        ca_data = None
        if data.get("ca_pem_file") is not None:
            pem = load_text(schema.required_string(data["ca_pem_file"], f"{label}: ca_pem_file"),
                            f"{label}: ca_pem_file", base_dir=base_dir)
            if "BEGIN CERTIFICATE" not in pem or "PRIVATE KEY" in pem:
                raise ValueError(f"{label}: ca_pem_file must contain only a public certificate.")
            ca_data = base64.b64encode(pem.strip().encode()).decode()
        rbac = ""
        if data.get("rbac_policies_file") is not None:
            text = load_text(schema.required_string(data["rbac_policies_file"], f"{label}: rbac_policies_file"),
                             f"{label}: rbac_policies_file", base_dir=base_dir)
            rbac = "\n".join(
                line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
            )
        sso_url = schema.string(data.get("sso_url"), f"{label}: sso_url")
        if enabled:
            if not sso_url or not sso_url.startswith("https://"):
                raise ValueError(f"{label}: sso_url must be the Identity Center application's https SAML URL.")
            if ca_data is None:
                raise ValueError(f"{label}: ca_pem_file (the application's signing certificate) is required.")
        return cls(
            enabled=enabled,
            sso_url=sso_url,
            sso_issuer=schema.string(data.get("sso_issuer"), f"{label}: sso_issuer"),
            ca_data=ca_data,
            connector_name=schema.string(data.get("connector_name"), f"{label}: connector_name",
                                         default="AWS IAM Identity Center") or "",
            username_attr=schema.string(data.get("username_attr"), f"{label}: username_attr", default="email") or "",
            email_attr=schema.string(data.get("email_attr"), f"{label}: email_attr", default="email") or "",
            groups_attr=schema.string(data.get("groups_attr"), f"{label}: groups_attr", default="groups") or "",
            name_id_policy_format=schema.string(
                data.get("name_id_policy_format"), f"{label}: name_id_policy_format",
                default="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            ) or "",
            rbac_default_policy=schema.string(
                data.get("rbac_default_policy"), f"{label}: rbac_default_policy",
                default="role:readonly", allow_empty=True,
            ) or "",
            rbac_policies=rbac,
        )


@dataclass(frozen=True)
class ArgoCdProps(ChartProps):
    host: str = ""
    server_replicas: int = 1
    ha: bool = False
    ingress: LoadBalancerProps = LoadBalancerProps()
    sso: ArgoCdSsoProps = ArgoCdSsoProps()

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, defaults: Mapping[str, Any], *, base_dir: Path | None) -> ArgoCdProps | None:
        if raw is None:
            return None
        label = f"EKS add-ons '{cluster}', argocd"
        data = schema.mapping(raw, label, {*cls.KEYS, "host", "server_replicas", "ha", "ingress", "sso"},
                              required=("host",))
        return cls(
            **ChartProps.parse(data, label, version="10.10.5", namespace="argocd", release="argocd", base_dir=base_dir),
            host=_host(data["host"], f"{label}: host"),
            server_replicas=schema.integer(data.get("server_replicas"), f"{label}: server_replicas",
                                           default=1, minimum=1, maximum=10) or 1,
            ha=schema.boolean(data.get("ha"), f"{label}: ha"),
            ingress=LoadBalancerProps.from_dict(data.get("ingress"), f"{label}: ingress", defaults),
            sso=ArgoCdSsoProps.from_dict(data.get("sso"), f"{label}: sso", base_dir=base_dir),
        )


@dataclass(frozen=True)
class NginxIngressProps(ChartProps):
    """F5 NGINX Ingress Controller (the maintained successor to retired ingress-nginx)."""

    name: str = ""
    ingress_class_name: str = "nginx"
    default_class: bool = False
    replica_count: int = 2
    service: LoadBalancerProps = LoadBalancerProps()

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, defaults: Mapping[str, Any], *, base_dir: Path | None) -> NginxIngressProps:
        data = schema.mapping(raw, f"EKS add-ons '{cluster}' nginx_ingress", {
            *cls.KEYS, "name", "ingress_class_name", "default_class", "replica_count", "service",
        }, required=("name",))
        name = _k8s_name(data["name"], f"EKS add-ons '{cluster}' nginx_ingress name")
        label = f"EKS add-ons '{cluster}', nginx_ingress '{name}'"
        return cls(
            **ChartProps.parse(data, label, version="2.8.0", namespace=f"nginx-ingress-{name}",
                               release=f"nginx-ingress-{name}", base_dir=base_dir),
            name=name,
            ingress_class_name=_k8s_name(data.get("ingress_class_name"), f"{label}: ingress_class_name", name),
            default_class=schema.boolean(data.get("default_class"), f"{label}: default_class"),
            replica_count=schema.integer(data.get("replica_count"), f"{label}: replica_count",
                                         default=2, minimum=1, maximum=20) or 2,
            service=LoadBalancerProps.from_dict(data.get("service"), f"{label}: service", defaults),
        )


@dataclass(frozen=True)
class KarpenterNodePoolProps:
    instance_categories: tuple[str, ...] = ("c", "m", "r", "t")
    capacity_types: tuple[str, ...] = ("on-demand",)
    architectures: tuple[str, ...] = ("amd64",)
    cpu_limit: int = 16
    consolidate_after: str = "1m"
    expire_after: str = "720h"

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> KarpenterNodePoolProps:
        data = schema.mapping(raw or {}, label, {
            "instance_categories", "capacity_types", "architectures", "cpu_limit",
            "consolidate_after", "expire_after",
        })
        def subset(key: str, allowed: tuple[str, ...] | None, default: tuple[str, ...]) -> tuple[str, ...]:
            values = schema.strings(data.get(key), f"{label}: {key}") or default
            if allowed is not None and set(values) - set(allowed):
                raise ValueError(f"{label}: {key} must be from {', '.join(allowed)}.")
            return values
        durations = {}
        for key, default in (("consolidate_after", "1m"), ("expire_after", "720h")):
            value = schema.string(data.get(key), f"{label}: {key}", default=default) or default
            if not DURATION.fullmatch(value):
                raise ValueError(f"{label}: {key} must be a duration such as 30s, 1m, 720h or Never.")
            durations[key] = value
        return cls(
            instance_categories=subset("instance_categories", None, ("c", "m", "r", "t")),
            capacity_types=subset("capacity_types", CAPACITY_TYPES, ("on-demand",)),
            architectures=subset("architectures", ARCHITECTURES, ("amd64",)),
            cpu_limit=schema.integer(data.get("cpu_limit"), f"{label}: cpu_limit",
                                     default=16, minimum=1, maximum=10000) or 16,
            **durations,
        )


@dataclass(frozen=True)
class KarpenterProps(ChartProps):
    node_role: str = ""
    replicas: int = 2
    subnet_tier: str = "public"
    security_groups: tuple[str, ...] = ()
    policy: Mapping[str, Any] = field(default_factory=dict)
    node_pool: KarpenterNodePoolProps = KarpenterNodePoolProps()
    create_node_access_entry: bool = False

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, *, base_dir: Path | None) -> KarpenterProps | None:
        if raw is None:
            return None
        label = f"EKS add-ons '{cluster}', karpenter"
        data = schema.mapping(raw, label, {
            *cls.KEYS, "node_role", "replicas", "subnet_tier", "security_groups", "policy",
            "node_pool", "create_node_access_entry",
        }, required=("node_role",))
        policy = load_document(data.get("policy", DEFAULT_KARPENTER_POLICY), f"{label}: policy", base_dir=base_dir)
        if not isinstance(policy, Mapping) or not policy.get("Statement"):
            raise ValueError(f"{label}: policy must be an IAM policy document.")
        return cls(
            **ChartProps.parse(data, label, version="1.15.0", namespace="kube-system", release="karpenter", base_dir=base_dir),
            node_role=schema.name(data["node_role"], f"{label}: node_role"),
            replicas=schema.integer(data.get("replicas"), f"{label}: replicas", default=2, minimum=1, maximum=5) or 2,
            subnet_tier=_choice(data.get("subnet_tier"), f"{label}: subnet_tier", SUBNET_TIERS, "public"),
            security_groups=tuple(
                schema.name(item, f"{label}: security_groups")
                for item in schema.strings(data.get("security_groups"), f"{label}: security_groups")
            ),
            policy=json_compatible(policy, f"{label}: policy"),
            node_pool=KarpenterNodePoolProps.from_dict(data.get("node_pool"), f"{label}: node_pool"),
            create_node_access_entry=schema.boolean(
                data.get("create_node_access_entry"), f"{label}: create_node_access_entry"
            ),
        )


@dataclass(frozen=True)
class HostedChartProps(ChartProps):
    """A chart with an optional ALB ingress (Kubecost, AWX)."""

    host: str | None = None
    ingress: LoadBalancerProps = LoadBalancerProps()
    create_instance: bool = False

    @classmethod
    def from_dict(
        cls, raw: Any, cluster: str, key: str, defaults: Mapping[str, Any], *,
        version: str, namespace: str, release: str, base_dir: Path | None, instance: bool = False,
    ) -> HostedChartProps | None:
        if raw is None:
            return None
        label = f"EKS add-ons '{cluster}', {key}"
        keys = {*cls.KEYS, "host", "ingress"} | ({"create_instance"} if instance else set())
        data = schema.mapping(raw, label, keys)
        host = _host(data["host"], f"{label}: host") if data.get("host") is not None else None
        if data.get("ingress") is not None and host is None:
            raise ValueError(f"{label}: ingress needs host.")
        create = schema.boolean(data.get("create_instance"), f"{label}: create_instance") if instance else False
        return cls(
            **ChartProps.parse(data, label, version=version, namespace=namespace, release=release, base_dir=base_dir),
            host=host,
            ingress=LoadBalancerProps.from_dict(data.get("ingress"), f"{label}: ingress", defaults),
            create_instance=create,
        )


@dataclass(frozen=True)
class GatewayApiProps(ChartProps):
    """NGINX Gateway Fabric plus the Gateway API standard CRDs."""

    gateway_class_name: str = "nginx"
    replicas: int = 1
    crds_chart: str = ""
    service: LoadBalancerProps | None = None

    @classmethod
    def from_dict(cls, raw: Any, cluster: str, defaults: Mapping[str, Any], *, base_dir: Path | None) -> GatewayApiProps | None:
        if raw is None:
            return None
        label = f"EKS add-ons '{cluster}', gateway_api"
        data = schema.mapping(raw, label, {*cls.KEYS, "gateway_class_name", "replicas", "crds_chart", "service"})
        crds = repository_path(
            schema.string(data.get("crds_chart"), f"{label}: crds_chart", default=DEFAULT_GATEWAY_CRDS) or "",
            f"{label}: crds_chart", base_dir=base_dir,
        )
        if not (crds / "Chart.yaml").is_file():
            raise ValueError(f"{label}: crds_chart '{crds}' must be a Helm chart directory.")
        return cls(
            **ChartProps.parse(data, label, version="2.8.0", namespace="nginx-gateway",
                               release="nginx-gateway-fabric", base_dir=base_dir),
            gateway_class_name=_k8s_name(data.get("gateway_class_name"), f"{label}: gateway_class_name", "nginx"),
            replicas=schema.integer(data.get("replicas"), f"{label}: replicas", default=1, minimum=1, maximum=10) or 1,
            crds_chart=str(crds),
            service=(
                LoadBalancerProps.from_dict(data["service"], f"{label}: service", defaults)
                if data.get("service") is not None else None
            ),
        )


@dataclass(frozen=True)
class StorageClassProps:
    name: str = "gp3"
    volume_type: str = "gp3"
    default: bool = True

    @classmethod
    def from_dict(cls, raw: Any, cluster: str) -> StorageClassProps | None:
        if raw is None:
            return None
        label = f"EKS add-ons '{cluster}', storage_class"
        data = schema.mapping(raw, label, {"name", "volume_type", "default"})
        return cls(
            name=_k8s_name(data.get("name"), f"{label}: name", "gp3"),
            volume_type=_choice(data.get("volume_type"), f"{label}: volume_type", ("gp3", "gp2", "io2"), "gp3"),
            default=schema.boolean(data.get("default"), f"{label}: default", default=True),
        )


@dataclass(frozen=True)
class ClusterAddonsProps:
    cluster: str
    addons: tuple[NativeAddonProps, ...] = ()
    load_balancer_controller: LoadBalancerControllerProps = LoadBalancerControllerProps()
    helm_charts: tuple[HelmChartProps, ...] = ()
    storage_class: StorageClassProps | None = None
    karpenter: KarpenterProps | None = None
    argocd: ArgoCdProps | None = None
    nginx_ingress: tuple[NginxIngressProps, ...] = ()
    gateway_api: GatewayApiProps | None = None
    kubecost: HostedChartProps | None = None
    awx_operator: HostedChartProps | None = None
    secrets_store_csi: SecretsStoreCsiProps | None = None
    secret_provider_classes: tuple[SecretProviderClassProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.addons or self.load_balancer_controller.enabled or self.helm_charts
            or self.storage_class or self.charts
        )

    @property
    def charts(self) -> tuple[ChartProps, ...]:
        """Enabled typed Helm add-ons."""
        candidates = (self.karpenter, self.argocd, *self.nginx_ingress, self.gateway_api,
                      self.kubecost, self.awx_operator, self.secrets_store_csi)
        return tuple(item for item in candidates if item is not None and item.enabled)

    @property
    def needs_pod_identity_agent(self) -> bool:
        return (
            self.load_balancer_controller.enabled
            or any(addon.pod_identity for addon in self.addons)
            or bool(self.karpenter and self.karpenter.enabled)
            or any(
                item.parameters.get("usePodIdentity", "false").lower() == "true"
                for item in self.secret_provider_classes
            )
        )

    @property
    def needs_load_balancer_controller(self) -> bool:
        exposed = [chart for chart in (self.argocd, self.kubecost, self.awx_operator) if chart and chart.enabled]
        return bool(
            [chart for chart in exposed if getattr(chart, "host", None)]
            or [item for item in self.nginx_ingress if item.enabled]
            or (self.gateway_api and self.gateway_api.enabled and self.gateway_api.service)
        )

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> ClusterAddonsProps:
        data = schema.mapping(raw, "eks_addons entry", {
            "cluster", "addons", "aws_load_balancer_controller", "helm_charts", "load_balancer_defaults",
            "storage_class", "karpenter", "argocd", "nginx_ingress", "gateway_api", "kubecost", "awx_operator",
            "secrets_store_csi", "secret_provider_classes",
        }, required=("cluster",))
        cluster = schema.name(data["cluster"], "eks_addons cluster")
        defaults = schema.mapping(
            data.get("load_balancer_defaults") or {}, f"EKS add-ons '{cluster}', load_balancer_defaults",
            {"scheme", "subnet_tier", "security_groups", "certificate", "certificate_arn", "ssl_policy", "group_name"},
        )
        props = cls(
            cluster=cluster,
            addons=schema.items(
                data.get("addons"), f"EKS add-ons '{cluster}' addons",
                lambda item: NativeAddonProps.from_dict(item, cluster, base_dir=base_dir),
            ),
            load_balancer_controller=LoadBalancerControllerProps.from_dict(
                data.get("aws_load_balancer_controller"), cluster, base_dir=base_dir
            ),
            helm_charts=schema.items(
                data.get("helm_charts"), f"EKS add-ons '{cluster}' helm_charts",
                lambda item: HelmChartProps.from_dict(item, cluster, base_dir=base_dir),
            ),
            storage_class=StorageClassProps.from_dict(data.get("storage_class"), cluster),
            secrets_store_csi=SecretsStoreCsiProps.from_dict(data.get("secrets_store_csi"), cluster, base_dir=base_dir),
            secret_provider_classes=schema.items(
                data.get("secret_provider_classes"), f"EKS add-ons '{cluster}' secret_provider_classes",
                lambda item: SecretProviderClassProps.from_dict(item, cluster),
                key=lambda item: f"{item.namespace}/{item.name}",
            ),
            karpenter=KarpenterProps.from_dict(data.get("karpenter"), cluster, base_dir=base_dir),
            argocd=ArgoCdProps.from_dict(data.get("argocd"), cluster, defaults, base_dir=base_dir),
            nginx_ingress=schema.items(
                data.get("nginx_ingress"), f"EKS add-ons '{cluster}' nginx_ingress",
                lambda item: NginxIngressProps.from_dict(item, cluster, defaults, base_dir=base_dir),
            ),
            gateway_api=GatewayApiProps.from_dict(data.get("gateway_api"), cluster, defaults, base_dir=base_dir),
            kubecost=HostedChartProps.from_dict(
                data.get("kubecost"), cluster, "kubecost", defaults,
                version="2.9.7", namespace="kubecost", release="kubecost", base_dir=base_dir,
            ),
            awx_operator=HostedChartProps.from_dict(
                data.get("awx_operator"), cluster, "awx_operator", defaults,
                version="3.2.1", namespace="awx", release="awx-operator", base_dir=base_dir, instance=True,
            ),
        )
        props.validate()
        return props

    def validate(self) -> None:
        props = self
        cluster = self.cluster
        label = f"EKS add-ons '{cluster}'"
        names = {addon.name for addon in props.addons}
        secrets_helm = bool(props.secrets_store_csi and props.secrets_store_csi.enabled)
        secrets_native = "aws-secrets-store-csi-driver-provider" in names
        if secrets_helm and secrets_native:
            raise ValueError(f"{label}: choose secrets_store_csi Helm or the native secrets provider add-on, not both.")
        if props.secret_provider_classes and not (secrets_helm or secrets_native):
            raise ValueError(f"{label}: secret_provider_classes need secrets_store_csi or the native secrets provider add-on.")
        if props.needs_pod_identity_agent and POD_IDENTITY_AGENT not in names:
            raise ValueError(f"{label}: Pod Identity roles need the {POD_IDENTITY_AGENT} add-on.")
        if props.needs_load_balancer_controller and not props.load_balancer_controller.enabled:
            raise ValueError(f"{label}: ingresses and NLB services need aws_load_balancer_controller enabled.")
        if props.storage_class is not None and "aws-ebs-csi-driver" not in names:
            raise ValueError(f"{label}: storage_class needs the aws-ebs-csi-driver add-on.")
        classes = [item.ingress_class_name for item in props.nginx_ingress if item.enabled]
        if len(classes) != len(set(classes)) or "alb" in classes:
            raise ValueError(f"{label}: nginx_ingress classes must be unique and not 'alb'.")
        if sum(item.default_class for item in props.nginx_ingress if item.enabled) > 1:
            raise ValueError(f"{label}: only one nginx_ingress can be the default class.")
        releases = [(chart.namespace, chart.release) for chart in props.helm_charts]
        if props.load_balancer_controller.enabled:
            releases.append((props.load_balancer_controller.namespace, "aws-load-balancer-controller"))
        releases += [(chart.namespace, chart.release) for chart in props.charts]
        if len(releases) != len(set(releases)):
            raise ValueError(f"{label}: duplicate Helm release in a namespace.")


@dataclass(frozen=True)
class EksAddonsProps:
    clusters: tuple[ClusterAddonsProps, ...] = ()

    def __bool__(self) -> bool:
        return any(self.clusters)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EksAddonsProps:
        return cls(clusters=schema.items(
            raw, "eks_addons",
            lambda item: ClusterAddonsProps.from_dict(item, base_dir=base_dir),
            key=lambda item: item.cluster,
        ))


def configuration_json(values: Mapping[str, Any]) -> str | None:
    return json.dumps(values, separators=(",", ":"), sort_keys=True) if values else None
