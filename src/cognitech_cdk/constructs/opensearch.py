"""Config-driven OpenSearch domains, mirroring the Terraform `AWS-OpenSearch` module.

```yaml
opensearch_domains:
  source_ip_cidr: 203.0.113.10/32          # formation `opensearch_source_ip_cidr`
  domains:
    - name: es-logs
      engine_version: OpenSearch_2.11
      cluster_config:
        instance_type: t3.small.search
        instance_count: 2
        zone_awareness_enabled: false
      ebs_options: {volume_size: 10}
      advanced_security_options:
        internal_user_database_enabled: true
        master_user_options: {master_user_name: admin}   # password generated in Secrets Manager
      access_policies: policies/opensearch-access.json   # [[placeholders]] are rendered
```

A plain list of domains is also accepted. Encryption at rest, node-to-node
encryption and HTTPS with TLS 1.2 are always on. Access policies that allow
`Principal: "*"` must carry a restrictive condition such as `aws:SourceIp`.

Resource names (OpenSearch caps domain names at 28 characters, so the
abbreviated account name is used, as in the Terraform module):

    {account_name_abr}-{region_prefix}-{vpc_name}-{name}
    /aws/opensearch/{domain_name}/{index-slow|search-slow|es-application|audit}-logs
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common import documents, schema
from cognitech_cdk.common.config import ORDINALS, Common
from cognitech_cdk.constructs.lookups import sso_role_arn
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.constructs.rds import (
    KMS_ARN,
    SUBNET_TYPES,
    _choice,
    _pattern,
    construct_id,
    resolve_security_groups,
    resolve_subnets,
)

LOG_RETENTION_DAYS = (
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731,
    1096, 1827, 2192, 2557, 2922, 3288, 3653,
)
TLS_POLICIES = {
    "Policy-Min-TLS-1-2-2019-07": opensearch.TLSSecurityPolicy.TLS_1_2,
    "Policy-Min-TLS-1-2-PFS-2023-10": opensearch.TLSSecurityPolicy.TLS_1_2_PFS,
}
VOLUME_TYPES = ("gp2", "gp3", "io1", "standard")
REMOVAL_POLICIES = {"retain": cdk.RemovalPolicy.RETAIN, "destroy": cdk.RemovalPolicy.DESTROY}
ENGINE_VERSION = re.compile(r"(OpenSearch|Elasticsearch)_(\d+\.\d+)")
INSTANCE_TYPE = re.compile(r"[a-z0-9]+\.[a-z0-9]+\.search")
DOMAIN_NAME = re.compile(r"[a-z][a-z0-9-]{2,27}")
CERTIFICATE_ARN = re.compile(r"arn:aws[a-z-]*:acm:[a-z0-9-]+:\d{12}:certificate/[A-Za-z0-9-]+")
IAM_ARN = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:(?:role|user)/[\w+=,.@/-]+")
HOSTNAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
# A condition on one of these keys narrows an otherwise anonymous principal.
RESTRICTIVE_CONDITION_KEYS = {
    "aws:sourceip", "aws:sourcevpc", "aws:sourcevpce", "aws:principalorgid",
    "aws:principalaccount", "aws:principalarn", "aws:sourceaccount", "aws:sourcearn",
    "aws:principalorgpaths", "aws:vpcsourceip",
}
LOG_TYPES = {
    "index_slow_logs_enabled": ("index-slow-logs", "slow_index_log"),
    "search_slow_logs_enabled": ("search-slow-logs", "slow_search_log"),
    "es_application_logs_enabled": ("es-application-logs", "app_log"),
    "audit_logs_enabled": ("audit-logs", "audit_log"),
}
PLACEHOLDERS = (
    "account_number", "source_ip", "resource_name", "region", "admin_role_arn",
    "domain_arn", "domain_name", "name_abr", "region_prefix",
)


def retention_days(value: Any, label: str, default: int) -> int:
    return _choice(value, label, LOG_RETENTION_DAYS, default)


def log_group(scope: Construct, cid: str, name: str, days: int) -> logs.LogGroup:
    """A retained log group with an exact retention, including values the enum lacks."""
    group = logs.LogGroup(scope, cid, log_group_name=name, removal_policy=cdk.RemovalPolicy.RETAIN)
    cfn = group.node.default_child
    if isinstance(cfn, logs.CfnLogGroup):
        cfn.retention_in_days = days
    cdk.Tags.of(group).add("Name", name)
    return group


@dataclass(frozen=True)
class VpcPlacementProps:
    """Where a VPC-attached resource lives: one subnet tier plus security groups."""

    security_groups: tuple[str, ...]
    subnet_type: str = "private"
    subnet_positions: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> VpcPlacementProps:
        data = schema.mapping(
            raw, label, {"subnet_type", "subnet_positions", "security_groups"},
            required=("security_groups",),
        )
        positions = schema.strings(data.get("subnet_positions"), f"{label}: subnet_positions")
        for position in positions:
            _choice(position, f"{label}: subnet_positions", ORDINALS, None)
        if len(set(positions)) != len(positions):
            raise ValueError(f"{label}: subnet_positions repeats a position.")
        return cls(
            security_groups=schema.strings(
                data["security_groups"], f"{label}: security_groups", allow_empty=False
            ),
            subnet_type=_choice(
                data.get("subnet_type"), f"{label}: subnet_type", SUBNET_TYPES, "private"
            ),
            subnet_positions=positions,
        )

    def resolve(
        self,
        scope: Construct,
        network: Network | None,
        security_groups: Mapping[str, ec2.ISecurityGroup] | None,
        label: str,
        *,
        count: int | None = None,
    ) -> tuple[list[ec2.ISubnet], list[ec2.ISecurityGroup]]:
        if network is None:
            raise ValueError(f"{label}: VPC placement needs the network dependency.")
        subnets = resolve_subnets(network, self.subnet_type, self.subnet_positions, label)
        if count is not None:
            if self.subnet_positions and len(subnets) != count:
                raise ValueError(f"{label}: expected {count} subnet_positions, got {len(subnets)}.")
            if len(subnets) < count:
                raise ValueError(
                    f"{label}: needs {count} {self.subnet_type} subnets, the network has "
                    f"{len(subnets)}."
                )
            subnets = subnets[:count]
        groups = resolve_security_groups(scope, security_groups or {}, self.security_groups, label)
        return subnets, groups


def _statements(document: Mapping[str, Any]) -> list[Any]:
    value = document.get("Statement")
    return [value] if isinstance(value, Mapping) else list(value or [])


def check_access_policy(document: Any, label: str) -> Mapping[str, Any]:
    """Validate shape and reject anonymous grants that carry no narrowing condition."""
    data = schema.mapping(document, label, {"Version", "Id", "Statement"}, required=("Statement",))
    statements = _statements(data)
    if not statements:
        raise ValueError(f"{label}: Statement must not be empty.")
    for index, statement in enumerate(statements, start=1):
        where = f"{label}, statement {index}"
        statement = schema.mapping(
            statement, where,
            {"Sid", "Effect", "Principal", "NotPrincipal", "Action", "NotAction",
             "Resource", "NotResource", "Condition"},
        )
        if statement.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f"{where}: Effect must be Allow or Deny.")
        if "NotPrincipal" in statement and statement.get("Effect") == "Allow":
            raise ValueError(f"{where}: Allow with NotPrincipal grants access to everyone else.")
        if statement.get("Effect") != "Allow" or not _is_anonymous(statement.get("Principal")):
            continue
        keys = {
            str(key).lower()
            for values in (statement.get("Condition") or {}).values()
            if isinstance(values, Mapping)
            for key in values
        }
        if not keys & RESTRICTIVE_CONDITION_KEYS:
            raise ValueError(
                f"{where}: Principal '*' needs a restrictive condition "
                f"({', '.join(sorted(RESTRICTIVE_CONDITION_KEYS))}); "
                "public access policies are not allowed."
            )
    return data


def _is_anonymous(principal: Any) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, Mapping):
        values = principal.get("AWS")
        values = [values] if isinstance(values, str) else values or []
        return "*" in values
    return False


def _cidr(value: str, label: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as error:
        raise ValueError(f"{label}: '{value}' is not a valid CIDR ({error}).") from None
    if network.prefixlen == 0:
        raise ValueError(f"{label}: '{value}' would open the domain to the whole internet.")
    return str(network)


@dataclass(frozen=True)
class ClusterConfigProps:
    instance_type: str = "r6g.large.search"
    instance_count: int = 2
    dedicated_master_enabled: bool = False
    dedicated_master_type: str | None = None
    dedicated_master_count: int | None = None
    zone_awareness_enabled: bool = True
    availability_zone_count: int = 2
    warm_enabled: bool = False
    warm_type: str | None = None
    warm_count: int | None = None
    multi_az_with_standby_enabled: bool = False

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ClusterConfigProps:
        data = schema.mapping(raw or {}, label, set(cls.__dataclass_fields__))

        def instance(key: str, default: str | None = None) -> str | None:
            return _pattern(
                data.get(key), f"{label}: {key}", INSTANCE_TYPE, "like r6g.large.search"
            ) or default

        master = schema.boolean(
            data.get("dedicated_master_enabled"), f"{label}: dedicated_master_enabled"
        )
        master_type = instance("dedicated_master_type")
        master_count = schema.integer(
            data.get("dedicated_master_count"), f"{label}: dedicated_master_count", minimum=1
        )
        if master and (not master_type or master_count not in (3, 5)):
            raise ValueError(
                f"{label}: dedicated masters need dedicated_master_type and a "
                "dedicated_master_count of 3 or 5."
            )
        if not master and (master_type or master_count):
            raise ValueError(
                f"{label}: dedicated master settings need dedicated_master_enabled: true."
            )
        warm = schema.boolean(data.get("warm_enabled"), f"{label}: warm_enabled")
        warm_type = instance("warm_type")
        warm_count = schema.integer(data.get("warm_count"), f"{label}: warm_count", minimum=2)
        if warm and (not warm_type or warm_count is None or not master):
            raise ValueError(
                f"{label}: warm storage needs warm_type, warm_count and dedicated masters."
            )
        if not warm and (warm_type or warm_count):
            raise ValueError(f"{label}: warm settings need warm_enabled: true.")
        zone_awareness = schema.boolean(
            data.get("zone_awareness_enabled"), f"{label}: zone_awareness_enabled", default=True
        )
        az_count = _choice(
            data.get("availability_zone_count"), f"{label}: availability_zone_count", (2, 3), 2
        )
        count = schema.integer(
            data.get("instance_count"), f"{label}: instance_count", default=2, minimum=1
        )
        if zone_awareness and count % az_count:
            raise ValueError(
                f"{label}: instance_count must be a multiple of availability_zone_count "
                "when zone awareness is enabled."
            )
        return cls(
            instance_type=instance("instance_type", "r6g.large.search"),
            instance_count=count,
            dedicated_master_enabled=master,
            dedicated_master_type=master_type,
            dedicated_master_count=master_count,
            zone_awareness_enabled=zone_awareness,
            availability_zone_count=az_count,
            warm_enabled=warm,
            warm_type=warm_type,
            warm_count=warm_count,
            multi_az_with_standby_enabled=schema.boolean(
                data.get("multi_az_with_standby_enabled"),
                f"{label}: multi_az_with_standby_enabled",
            ),
        )


@dataclass(frozen=True)
class EbsOptionsProps:
    ebs_enabled: bool = True
    volume_type: str = "gp3"
    volume_size: int = 100
    iops: int | None = None
    throughput: int | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> EbsOptionsProps:
        data = schema.mapping(raw or {}, label, set(cls.__dataclass_fields__))
        volume_type = _choice(
            data.get("volume_type"), f"{label}: volume_type", VOLUME_TYPES, "gp3"
        )
        iops = schema.integer(data.get("iops"), f"{label}: iops", minimum=1000)
        throughput = schema.integer(data.get("throughput"), f"{label}: throughput", minimum=125)
        if volume_type == "gp3":
            iops = 3000 if iops is None else iops
            throughput = 125 if throughput is None else throughput
        elif throughput is not None:
            raise ValueError(f"{label}: throughput is only valid for gp3 volumes.")
        if volume_type == "io1" and iops is None:
            raise ValueError(f"{label}: io1 volumes need iops.")
        if volume_type in ("gp2", "standard") and iops is not None:
            raise ValueError(f"{label}: iops is not valid for {volume_type} volumes.")
        return cls(
            ebs_enabled=schema.boolean(
                data.get("ebs_enabled"), f"{label}: ebs_enabled", default=True
            ),
            volume_type=volume_type,
            volume_size=schema.integer(
                data.get("volume_size"), f"{label}: volume_size", default=100, minimum=10
            ),
            iops=iops,
            throughput=throughput,
        )


@dataclass(frozen=True)
class FineGrainedAccessProps:
    master_user_arn: str | None = None
    master_user_name: str | None = None
    master_user_secret: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> FineGrainedAccessProps | None:
        data = schema.mapping(
            raw, label,
            {"enabled", "anonymous_auth_enabled", "internal_user_database_enabled",
             "master_user_options"},
        )
        if not schema.boolean(data.get("enabled"), f"{label}: enabled", default=True):
            return None
        if schema.boolean(data.get("anonymous_auth_enabled"), f"{label}: anonymous_auth_enabled"):
            raise ValueError(f"{label}: anonymous_auth_enabled is not allowed.")
        where = f"{label}: master_user_options"
        options = schema.mapping(
            data.get("master_user_options"), where,
            {"master_user_arn", "master_user_name", "master_user_password", "master_user_secret"},
        )
        if "master_user_password" in options:
            raise ValueError(
                f"{where}: master_user_password would put a password in the template; use "
                "master_user_secret, or omit it to have Secrets Manager generate one."
            )
        arn = _pattern(
            options.get("master_user_arn"), f"{where}: master_user_arn", IAM_ARN,
            "an IAM role or user ARN",
        )
        user = schema.string(options.get("master_user_name"), f"{where}: master_user_name")
        secret = options.get("master_user_secret")
        if secret is not None:
            secret = schema.name(secret, f"{where}: master_user_secret")
        if bool(arn) == bool(user or secret):
            raise ValueError(
                f"{where}: set master_user_arn, or master_user_name/master_user_secret, not both."
            )
        internal = data.get("internal_user_database_enabled")
        if internal is not None and schema.boolean(
            internal, f"{label}: internal_user_database_enabled"
        ) != bool(user or secret):
            raise ValueError(
                f"{label}: internal_user_database_enabled must be true with a master user name "
                "and false with master_user_arn."
            )
        return cls(master_user_arn=arn, master_user_name=user, master_user_secret=secret)


@dataclass(frozen=True)
class OpenSearchDomainProps:
    name: str
    create: bool = True
    engine_version: str = "OpenSearch_2.11"
    cluster_config: ClusterConfigProps = ClusterConfigProps()
    ebs_options: EbsOptionsProps = EbsOptionsProps()
    encryption_kms_key_arn: str | None = None
    tls_security_policy: str = "Policy-Min-TLS-1-2-2019-07"
    custom_endpoint: str | None = None
    custom_endpoint_certificate: str | None = None
    custom_endpoint_certificate_arn: str | None = None
    vpc_options: VpcPlacementProps | None = None
    fine_grained_access: FineGrainedAccessProps | None = None
    access_policies: Mapping[str, Any] | None = None
    source_ip_cidr: str | None = None
    advanced_options: Mapping[str, str] = field(default_factory=dict)
    log_publishing: Mapping[str, bool] = field(
        default_factory=lambda: {key: key == "es_application_logs_enabled" for key in LOG_TYPES}
    )
    log_retention_days: int = 14
    removal_policy: str = "retain"
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> OpenSearchDomainProps:
        data = schema.mapping(
            raw, "OpenSearch domain",
            {"name", "create", "engine_version", "cluster_config", "ebs_options",
             "encrypt_at_rest", "node_to_node_encryption", "domain_endpoint_options",
             "vpc_options", "advanced_security_options", "access_policies", "source_ip_cidr",
             "advanced_options", "log_publishing", "removal_policy", "tags"},
            required=("name",),
        )
        name = schema.name(data["name"], "OpenSearch domain name")
        label = f"OpenSearch domain '{name}'"
        if name != name.lower():
            raise ValueError(f"{label}: name must be lowercase.")
        version = (
            schema.string(data.get("engine_version"), f"{label}: engine_version")
            or "OpenSearch_2.11"
        )
        if not ENGINE_VERSION.fullmatch(version):
            raise ValueError(f"{label}: engine_version must look like OpenSearch_2.11.")

        encrypt = schema.mapping(
            data.get("encrypt_at_rest") or {}, f"{label}: encrypt_at_rest",
            {"enabled", "kms_key_id"},
        )
        if not schema.boolean(
            encrypt.get("enabled"), f"{label}: encrypt_at_rest.enabled", default=True
        ):
            raise ValueError(f"{label}: encryption at rest cannot be disabled.")
        if not schema.boolean(
            data.get("node_to_node_encryption"), f"{label}: node_to_node_encryption", default=True
        ):
            raise ValueError(f"{label}: node-to-node encryption cannot be disabled.")

        endpoint = schema.mapping(
            data.get("domain_endpoint_options") or {}, f"{label}: domain_endpoint_options",
            {"enforce_https", "tls_security_policy", "custom_endpoint_enabled", "custom_endpoint",
             "custom_endpoint_certificate", "custom_endpoint_certificate_arn"},
        )
        where = f"{label}: domain_endpoint_options"
        if not schema.boolean(
            endpoint.get("enforce_https"), f"{where}.enforce_https", default=True
        ):
            raise ValueError(f"{label}: HTTPS cannot be disabled.")
        tls = _choice(
            endpoint.get("tls_security_policy"), f"{where}.tls_security_policy",
            tuple(TLS_POLICIES), "Policy-Min-TLS-1-2-2019-07",
        )
        custom_enabled = schema.boolean(
            endpoint.get("custom_endpoint_enabled"), f"{where}.custom_endpoint_enabled",
            default="custom_endpoint" in endpoint,
        )
        custom = _pattern(
            endpoint.get("custom_endpoint"), f"{where}.custom_endpoint", HOSTNAME,
            "a lowercase DNS name",
        )
        certificate = endpoint.get("custom_endpoint_certificate")
        if certificate is not None:
            certificate = schema.name(certificate, f"{where}.custom_endpoint_certificate")
        certificate_arn = _pattern(
            endpoint.get("custom_endpoint_certificate_arn"),
            f"{where}.custom_endpoint_certificate_arn", CERTIFICATE_ARN, "an ACM certificate ARN",
        )
        if custom_enabled != bool(custom):
            raise ValueError(f"{where}: custom_endpoint_enabled requires custom_endpoint.")
        if custom and bool(certificate) == bool(certificate_arn):
            raise ValueError(
                f"{where}: a custom endpoint needs exactly one of custom_endpoint_certificate "
                "or custom_endpoint_certificate_arn."
            )
        if not custom and (certificate or certificate_arn):
            raise ValueError(f"{where}: a certificate needs custom_endpoint.")

        cluster = ClusterConfigProps.from_dict(
            data.get("cluster_config"), f"{label}: cluster_config"
        )
        vpc_options = (
            VpcPlacementProps.from_dict(data["vpc_options"], f"{label}: vpc_options")
            if data.get("vpc_options") is not None else None
        )
        if vpc_options and vpc_options.subnet_positions:
            wanted = cluster.availability_zone_count if cluster.zone_awareness_enabled else 1
            if len(vpc_options.subnet_positions) != wanted:
                raise ValueError(
                    f"{label}: vpc_options.subnet_positions needs {wanted} position(s) to match "
                    "the zone awareness settings."
                )
        fgac = (
            FineGrainedAccessProps.from_dict(
                data["advanced_security_options"], f"{label}: advanced_security_options"
            )
            if data.get("advanced_security_options") is not None else None
        )

        policy = None
        if data.get("access_policies") is not None:
            loaded = documents.load_document(
                data["access_policies"], f"{label}: access_policies", base_dir=base_dir
            )
            unknown = documents.placeholders(loaded) - set(PLACEHOLDERS)
            if unknown:
                raise ValueError(
                    f"{label}: access_policies uses unknown placeholders: "
                    f"{', '.join(sorted(unknown))}."
                )
            policy = documents.json_compatible(
                check_access_policy(loaded, f"{label}: access_policies"),
                f"{label}: access_policies",
            )
        source_ip = schema.string(data.get("source_ip_cidr"), f"{label}: source_ip_cidr")
        if source_ip is not None:
            source_ip = _cidr(source_ip, f"{label}: source_ip_cidr")

        logging = schema.mapping(
            data.get("log_publishing") or {}, f"{label}: log_publishing",
            {*LOG_TYPES, "log_retention_days"},
        )
        log_publishing = {
            key: schema.boolean(
                logging.get(key), f"{label}: log_publishing.{key}",
                default=key == "es_application_logs_enabled",
            )
            for key in LOG_TYPES
        }
        if log_publishing["audit_logs_enabled"] and fgac is None:
            raise ValueError(f"{label}: audit logs require advanced_security_options.")

        return cls(
            name=name,
            create=schema.boolean(data.get("create"), f"{label}: create", default=True),
            engine_version=version,
            cluster_config=cluster,
            ebs_options=EbsOptionsProps.from_dict(
                data.get("ebs_options"), f"{label}: ebs_options"
            ),
            encryption_kms_key_arn=_pattern(
                encrypt.get("kms_key_id"), f"{label}: encrypt_at_rest.kms_key_id", KMS_ARN,
                "a KMS key ARN",
            ),
            tls_security_policy=tls,
            custom_endpoint=custom,
            custom_endpoint_certificate=certificate,
            custom_endpoint_certificate_arn=certificate_arn,
            vpc_options=vpc_options,
            fine_grained_access=fgac,
            access_policies=policy,
            source_ip_cidr=source_ip,
            advanced_options=schema.string_map(
                data.get("advanced_options"), f"{label}: advanced_options"
            ),
            log_publishing=log_publishing,
            log_retention_days=retention_days(
                logging.get("log_retention_days"),
                f"{label}: log_publishing.log_retention_days", 14,
            ),
            removal_policy=_choice(
                data.get("removal_policy"), f"{label}: removal_policy",
                tuple(REMOVAL_POLICIES), "retain",
            ),
            tags=schema.string_map(data.get("tags"), f"{label}: tags"),
        )


@dataclass(frozen=True)
class OpenSearchProps:
    domains: tuple[OpenSearchDomainProps, ...] = ()
    source_ip_cidr: str | None = None

    def __bool__(self) -> bool:
        return any(domain.create for domain in self.domains)

    @property
    def enabled(self) -> tuple[OpenSearchDomainProps, ...]:
        return tuple(domain for domain in self.domains if domain.create)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> OpenSearchProps:
        """Accept a list of domains, or `{source_ip_cidr, domains}`."""
        source_ip = None
        if isinstance(raw, Mapping):
            data = schema.mapping(raw, "opensearch_domains", {"source_ip_cidr", "domains"})
            source_ip = schema.string(
                data.get("source_ip_cidr"), "opensearch_domains.source_ip_cidr"
            )
            if source_ip is not None:
                source_ip = _cidr(source_ip, "opensearch_domains.source_ip_cidr")
            raw = data.get("domains")
        domains = schema.items(
            raw, "opensearch_domains",
            lambda item: OpenSearchDomainProps.from_dict(item, base_dir=base_dir),
        )
        return cls(domains=domains, source_ip_cidr=source_ip)


def _engine(version: str) -> opensearch.EngineVersion:
    match = ENGINE_VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"Invalid OpenSearch engine version '{version}'.")
    family, number = match.groups()
    if family == "OpenSearch":
        return opensearch.EngineVersion.open_search(number)
    return opensearch.EngineVersion.elasticsearch(number)


class OpenSearchDomains(Construct):
    """One `opensearch.Domain` per enabled config entry."""

    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        common: Common,
        prefix: str,
        props: OpenSearchProps,
        network: Network | None = None,
        security_groups: Mapping[str, ec2.ISecurityGroup] | None = None,
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
        certificates: Mapping[str, acm.ICertificate] | None = None,
    ) -> None:
        super().__init__(scope, construct_id_)
        self.domains: dict[str, opensearch.Domain] = {}
        self.domain_names: dict[str, str] = {}
        self.master_user_secrets: dict[str, secretsmanager.ISecret] = {}
        self._admin_role_arn: str | None = None
        self._common = common
        for entry in props.enabled:
            self._add(entry, prefix, props.source_ip_cidr, network, security_groups or {},
                      secrets or {}, certificates or {})

    def _add(
        self,
        entry: OpenSearchDomainProps,
        prefix: str,
        source_ip: str | None,
        network: Network | None,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        secrets: Mapping[str, secretsmanager.ISecret],
        certificates: Mapping[str, acm.ICertificate],
    ) -> None:
        stack = cdk.Stack.of(self)
        label = f"OpenSearch domain '{entry.name}'"
        cid = construct_id(entry.name)
        domain_name = self._common.abbreviated_name(prefix, entry.name)
        if not DOMAIN_NAME.fullmatch(domain_name):
            raise ValueError(
                f"{label}: domain name '{domain_name}' must be 3-28 lowercase letters, digits "
                "or hyphens starting with a letter; shorten the name."
            )
        cluster = entry.cluster_config
        ebs = entry.ebs_options

        vpc_args: dict[str, Any] = {}
        if entry.vpc_options is not None:
            count = cluster.availability_zone_count if cluster.zone_awareness_enabled else 1
            subnets, groups = entry.vpc_options.resolve(
                self, network, security_groups, label, count=count
            )
            vpc_args = {
                "vpc": network.vpc if network else None,
                "vpc_subnets": [ec2.SubnetSelection(subnets=subnets)],
                "security_groups": groups,
            }

        fgac = None
        if entry.fine_grained_access is not None:
            options = entry.fine_grained_access
            password = None
            user = options.master_user_name
            if options.master_user_secret:
                secret = schema.lookup(
                    secrets, options.master_user_secret, f"{label} master_user_secret"
                )
                # Dynamic references: CloudFormation resolves them; the template never
                # holds the values.
                password = secret.secret_value_from_json("password")
                user = user or secret.secret_value_from_json("username").unsafe_unwrap()
                self.master_user_secrets[entry.name] = secret
            fgac = opensearch.AdvancedSecurityOptions(
                master_user_arn=options.master_user_arn,
                master_user_name=user,
                master_user_password=password,
            )

        custom_endpoint = None
        if entry.custom_endpoint:
            certificate = (
                schema.lookup(
                    certificates, entry.custom_endpoint_certificate,
                    f"{label} custom_endpoint_certificate",
                )
                if entry.custom_endpoint_certificate
                else acm.Certificate.from_certificate_arn(
                    self, f"{cid}Certificate", entry.custom_endpoint_certificate_arn
                )
            )
            custom_endpoint = opensearch.CustomEndpointOptions(
                domain_name=entry.custom_endpoint, certificate=certificate
            )

        logging_args: dict[str, Any] = {}
        for key, (suffix, attribute) in LOG_TYPES.items():
            if entry.log_publishing[key]:
                logging_args[f"{attribute}_enabled"] = True
                logging_args[f"{attribute}_group"] = log_group(
                    self, f"{cid}{construct_id(suffix)}",
                    f"/aws/opensearch/{domain_name}/{suffix}", entry.log_retention_days,
                )

        domain = opensearch.Domain(
            self,
            cid,
            domain_name=domain_name,
            version=_engine(entry.engine_version),
            capacity=opensearch.CapacityConfig(
                data_node_instance_type=cluster.instance_type,
                data_nodes=cluster.instance_count,
                master_node_instance_type=cluster.dedicated_master_type,
                master_nodes=cluster.dedicated_master_count,
                warm_instance_type=cluster.warm_type,
                warm_nodes=cluster.warm_count,
                multi_az_with_standby_enabled=cluster.multi_az_with_standby_enabled,
            ),
            zone_awareness=opensearch.ZoneAwarenessConfig(
                enabled=cluster.zone_awareness_enabled,
                availability_zone_count=(
                    cluster.availability_zone_count if cluster.zone_awareness_enabled else None
                ),
            ),
            ebs=opensearch.EbsOptions(
                enabled=ebs.ebs_enabled,
                volume_type=getattr(ec2.EbsDeviceVolumeType, ebs.volume_type.upper()),
                volume_size=ebs.volume_size if ebs.ebs_enabled else None,
                iops=ebs.iops if ebs.ebs_enabled else None,
                throughput=ebs.throughput if ebs.ebs_enabled else None,
            ),
            encryption_at_rest=opensearch.EncryptionAtRestOptions(
                enabled=True,
                kms_key=(
                    kms.Key.from_key_arn(self, f"{cid}Key", entry.encryption_kms_key_arn)
                    if entry.encryption_kms_key_arn else None
                ),
            ),
            node_to_node_encryption=True,
            enforce_https=True,
            tls_security_policy=TLS_POLICIES[entry.tls_security_policy],
            custom_endpoint=custom_endpoint,
            fine_grained_access_control=fgac,
            advanced_options=dict(entry.advanced_options) or None,
            logging=opensearch.LoggingOptions(**logging_args) if logging_args else None,
            # The scoped policy below replaces CDK's custom-resource-managed one.
            suppress_logs_resource_policy=bool(logging_args) or None,
            removal_policy=REMOVAL_POLICIES[entry.removal_policy],
            **vpc_args,
        )
        cdk.Tags.of(domain).add("Name", domain_name)
        for tag_key, value in entry.tags.items():
            cdk.Tags.of(domain).add(tag_key, value)

        if logging_args:
            policy = logs.CfnResourcePolicy(
                self,
                f"{cid}LogPolicy",
                policy_name=f"{domain_name}-log-policy",
                policy_document=stack.to_json_string({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Principal": {"Service": "es.amazonaws.com"},
                        "Action": ["logs:PutLogEvents", "logs:CreateLogStream"],
                        "Resource": (
                            f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:"
                            f"log-group:/aws/opensearch/{domain_name}/*"
                        ),
                        "Condition": {"StringEquals": {"aws:SourceAccount": stack.account}},
                    }],
                }),
            )
            domain.node.add_dependency(policy)

        if entry.access_policies is not None:
            document = documents.render(
                entry.access_policies,
                self._placeholders(entry, domain_name, source_ip),
                f"{label}: access_policies",
            )
            cfn_domain = domain.node.default_child
            if isinstance(cfn_domain, opensearch.CfnDomain):
                # Set directly, as Terraform does, instead of CDK's custom resource.
                cfn_domain.access_policies = document

        if entry.name not in self.master_user_secrets:
            generated = domain.node.try_find_child("MasterUser")
            if isinstance(generated, secretsmanager.Secret):
                generated.apply_removal_policy(REMOVAL_POLICIES[entry.removal_policy])
                self.master_user_secrets[entry.name] = generated

        self.domains[entry.name] = domain
        self.domain_names[entry.name] = domain_name

    def _placeholders(
        self, entry: OpenSearchDomainProps, domain_name: str, source_ip: str | None
    ) -> dict[str, Any]:
        stack = cdk.Stack.of(self)
        common = self._common
        account_abr = common.account_name_abr or common.account_name
        values: dict[str, Any] = {
            "account_number": stack.account,
            "region": stack.region,
            # Terraform policies spell the domain as
            # `[[name_abr]]-[[region_prefix]]-[[resource_name]]`; keep that working
            # with this repo's `{vpc_name}-{name}` suffix.
            "resource_name": domain_name.removeprefix(f"{account_abr}-{common.region_prefix}-"),
            "domain_name": domain_name,
            "domain_arn": (
                f"arn:{stack.partition}:es:{stack.region}:{stack.account}:domain/{domain_name}"
            ),
            "name_abr": account_abr,
            "region_prefix": common.region_prefix,
        }
        cidr = entry.source_ip_cidr or source_ip
        if cidr:
            values["source_ip"] = cidr
        if "admin_role_arn" in documents.placeholders(entry.access_policies):
            if self._admin_role_arn is None:
                self._admin_role_arn = sso_role_arn(self, "AdminRole", "AdministratorAccess")
            values["admin_role_arn"] = self._admin_role_arn
        return values
