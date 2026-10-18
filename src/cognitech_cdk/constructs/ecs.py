"""Config-driven ECS clusters, mirroring the Terraform `Deploy-ecs` module.

    ecs_clusters:
      - name: app                           # {account}-{region}-{network}-app
        container_insights: true
        capacity_providers:
          names: [app-cp, FARGATE]          # ec2_autoscaling.capacity_provider.name or FARGATE[_SPOT]
          default_strategy:
            - {capacity_provider: app-cp, base: 2, weight: 1}
        cloud_map_namespaces:
          - name: app.local                 # DNS_PRIVATE (default) | DNS_PUBLIC | HTTP
            services:
              - name: backend
                dns_config: {dns_records: [{type: A, ttl: 10}]}
                health_check_custom_config: {failure_threshold: 1}
        task_definitions:
          - family: backend
            task_role: ecs-task             # iam.roles key (or task_role_arn)
            execution_role: ecs-execution   # iam.roles key (or execution_role_arn)
            network_mode: awsvpc
            cpu: 512
            memory: 1024
            database_secret: db             # secrets key -> DB_PASSWORD/USERNAME/PORT/NAME
            container_definitions:          # or container_definitions_file: path.json
              - name: backend
                repository: backend         # ecr_repositories key (or image: ...)
                image_tag: latest
                port_mappings: [{container_port: 3000}]
                environment: {NODE_ENV: production}
                secrets: [{name: JWT_SECRET, secret: smtp, key: JWT_SECRET}]
                log_configuration: {log_driver: awslogs}
        services:
          - name: backend
            task_definition: backend
            desired_count: 2
            capacity_provider_strategy: [{capacity_provider: app-cp, weight: 1}]
            load_balancers: [{target_group: backend, container_name: backend, container_port: 3000}]
            network_configuration: {subnets: private, security_groups: [ecs-backend]}
            service_registries: {cloud_map_service: app.local/backend}
        ec2_autoscaling:
          launch_templates:
            - name: app
              ami: ECSAL2023                # ECSAL2023 | ECSAL2 | AL2023 | AL2, or ami_id / ami_ssm_parameter
              instance_type: t3.large
              instance_profile: ecs         # iam.instance_profiles key
              key_pair: app                 # key pair key
              security_groups: [ecs-instance]
          autoscaling_group: {name: app, min_size: 1, max_size: 3, subnets: private}
          capacity_provider: {name: app-cp, managed_scaling: {target_capacity: 80}}

Secrets are only ever referenced by ARN (`valueFrom`); container environment
variables whose names look secret are rejected so values never land in a
template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import aws_cdk as cdk
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_servicediscovery as servicediscovery
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.config import Common
from cognitech_cdk.common.documents import json_compatible, load_document, placeholders, render
from cognitech_cdk.constructs.ecr import logical_id
from cognitech_cdk.constructs.network import Network

LAUNCH_TYPES = ("EC2", "FARGATE", "EXTERNAL")
NETWORK_MODES = ("bridge", "host", "awsvpc", "none")
COMPATIBILITIES = ("EC2", "FARGATE", "EXTERNAL")
FARGATE_PROVIDERS = ("FARGATE", "FARGATE_SPOT")
SCHEDULING = ("REPLICA", "DAEMON")
PROPAGATE_TAGS = ("SERVICE", "TASK_DEFINITION", "NONE")
CONTROLLERS = ("ECS", "CODE_DEPLOY", "EXTERNAL")
TIERS = ("private", "public")
NAMESPACE_TYPES = ("DNS_PRIVATE", "DNS_PUBLIC", "HTTP")
DNS_RECORD_TYPES = ("A", "AAAA", "CNAME", "SRV")
AMIS = {
    "ECSAL2023": "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id",
    "ECSAL2": "/aws/service/ecs/optimized-ami/amazon-linux-2/recommended/image_id",
    "AL2023": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64",
    "AL2": "/aws/service/ami-amazon-linux-latest/amzn2-ami-kernel-5.10-hvm-x86_64-gp2",
}
LEGACY_TOKENS = ("__BACKEND_URL__", "__SECRET_ARN__", "__FRONTEND_URL__", "__SMTP_SECRET_ARN__")
FILE_PLACEHOLDERS = ("account_number", "region", "account_name_abr", "cluster_name")
REFERENCE_PLACEHOLDER = re.compile(r"(repository|secret):([A-Za-z0-9][A-Za-z0-9_-]*)")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SECRET_LIKE = re.compile(
    r"PASSWORD|PASSWD|SECRET|PRIVATE_KEY|API_KEY|ACCESS_KEY|TOKEN", re.IGNORECASE
)
DNS_NAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*")
ROLE_ARN = re.compile(r"arn:[a-z0-9-]+:iam::[0-9]{12}:role/[\w+=,.@/-]+")
TASK_DEFINITION_ARN = re.compile(r"arn:[a-z0-9-]+:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[\w-]+(?::[0-9]+)?")
DB_FIELDS = (("DB_PASSWORD", "password"), ("DB_USERNAME", "username"), ("DB_PORT", "port"), ("DB_NAME", "dbname"))
LOG_RETENTION_DAYS = (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192,
                      2557, 2922, 3288, 3653)
FREEFORM_KEYS = ("options", "dockerLabels")
RESOURCE_NAME = re.compile(r"[A-Za-z0-9_-]{1,255}")


# --- small validators -------------------------------------------------------


def _enum(value: Any, label: str, choices: tuple[str, ...], default: str | None = None) -> str | None:
    result = schema.string(value, label, default=default)
    if result is not None and result not in choices:
        raise ValueError(f"{label} must be one of {', '.join(choices)}.")
    return result


def _list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return value


def _number_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise ValueError(f"{label} must be a number or a string such as '1 vCPU'.")
    return str(value)


def _port(value: Any, label: str, *, required: bool = False) -> int | None:
    if value is None and required:
        raise ValueError(f"{label} is required.")
    return schema.integer(value, label, minimum=0 if not required else 1, maximum=65535)


def _env_name(name: Any, label: str) -> str:
    if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
        raise ValueError(f"{label}: environment variable names must be identifiers.")
    if SECRET_LIKE.search(name):
        raise ValueError(
            f"{label}: '{name}' looks like a secret; reference it under 'secrets' "
            "(Secrets Manager or SSM) instead of a plaintext environment value."
        )
    return name


def _strings_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for key, item in value.items() for text in _strings_in(key) + _strings_in(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings_in(item)]
    return []


def strategy_is_fargate(providers: tuple[str, ...]) -> bool | None:
    kinds = {provider in FARGATE_PROVIDERS for provider in providers}
    if len(kinds) > 1:
        return None
    return kinds.pop() if kinds else False


# --- props ------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyItem:
    capacity_provider: str
    weight: int = 1
    base: int = 0

    @classmethod
    def parse(cls, raw: Any, label: str) -> tuple[StrategyItem, ...]:
        result = []
        for entry in _list(raw, label):
            data = schema.mapping(
                entry, label, {"capacity_provider", "weight", "base"},
                required=("capacity_provider",),
            )
            result.append(cls(
                schema.required_string(data["capacity_provider"], f"{label}: capacity_provider"),
                schema.integer(data.get("weight"), f"{label}: weight", default=1, minimum=0, maximum=1000),
                schema.integer(data.get("base"), f"{label}: base", default=0, minimum=0, maximum=100000),
            ))
        names = [item.capacity_provider for item in result]
        if len(names) != len(set(names)):
            raise ValueError(f"{label}: duplicate capacity provider.")
        if result and strategy_is_fargate(tuple(names)) is None:
            raise ValueError(f"{label}: cannot mix Fargate and EC2 capacity providers.")
        if sum(1 for item in result if item.base) > 1:
            raise ValueError(f"{label}: only one capacity provider may set base.")
        return tuple(result)


@dataclass(frozen=True)
class ExecuteCommandProps:
    kms_key_arn: str | None = None
    logging: str = "DEFAULT"
    cloud_watch_encryption_enabled: bool = False
    cloud_watch_log_group_name: str | None = None
    s3_bucket_name: str | None = None
    s3_encryption_enabled: bool = False
    s3_key_prefix: str | None = None
    has_log_configuration: bool = False

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ExecuteCommandProps:
        data = schema.mapping(raw, label, {"kms_key_arn", "logging", "log_configuration"})
        logging = _enum(data.get("logging"), f"{label}: logging", ("NONE", "DEFAULT", "OVERRIDE"), "DEFAULT")
        log = data.get("log_configuration")
        if (logging == "OVERRIDE") != (log is not None):
            raise ValueError(f"{label}: log_configuration is required with, and only with, logging OVERRIDE.")
        log_data = schema.mapping(log or {}, f"{label}: log_configuration", {
            "cloud_watch_encryption_enabled", "cloud_watch_log_group_name",
            "s3_bucket_name", "s3_encryption_enabled", "s3_key_prefix",
        })
        where = f"{label}: log_configuration"
        return cls(
            kms_key_arn=schema.string(data.get("kms_key_arn"), f"{label}: kms_key_arn"),
            logging=logging or "DEFAULT",
            cloud_watch_encryption_enabled=schema.boolean(
                log_data.get("cloud_watch_encryption_enabled"), where, default=True
            ),
            cloud_watch_log_group_name=schema.string(log_data.get("cloud_watch_log_group_name"), where),
            s3_bucket_name=schema.string(log_data.get("s3_bucket_name"), where),
            s3_encryption_enabled=schema.boolean(log_data.get("s3_encryption_enabled"), where, default=True),
            s3_key_prefix=schema.string(log_data.get("s3_key_prefix"), where),
            has_log_configuration=log is not None,
        )


@dataclass(frozen=True)
class ContainerSecret:
    name: str
    value_from: str | None = None
    secret: str | None = None
    key: str | None = None

    @classmethod
    def parse(cls, raw: Any, label: str) -> tuple[ContainerSecret, ...]:
        result = []
        for entry in _list(raw, label):
            data = schema.mapping(entry, label, {"name", "value_from", "secret", "key"}, required=("name",))
            name = data["name"]
            if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
                raise ValueError(f"{label}: secret names must be identifiers.")
            if ("value_from" in data) == ("secret" in data):
                raise ValueError(f"{label} '{name}': provide exactly one of value_from or secret.")
            if "key" in data and "secret" not in data:
                raise ValueError(f"{label} '{name}': key is only valid with secret.")
            result.append(cls(
                name,
                schema.string(data.get("value_from"), f"{label} '{name}': value_from"),
                schema.string(data.get("secret"), f"{label} '{name}': secret"),
                schema.string(data.get("key"), f"{label} '{name}': key"),
            ))
        return tuple(result)


@dataclass(frozen=True)
class ContainerProps:
    """Inline container; `definition` holds ECS-API (camelCase) static fields."""

    name: str
    definition: Mapping[str, Any]
    image: str | None = None
    repository: str | None = None
    image_tag: str = "latest"
    environment: tuple[tuple[str, str], ...] = ()
    secrets: tuple[ContainerSecret, ...] = ()
    log_driver: str | None = None
    log_options: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ContainerProps:
        data = schema.mapping(raw, label, {
            "name", "image", "repository", "image_tag", "cpu", "memory",
            "memory_reservation", "essential", "command", "entry_point",
            "working_directory", "user", "readonly_root_filesystem", "stop_timeout",
            "port_mappings", "environment", "secrets", "mount_points",
            "log_configuration", "health_check",
        }, required=("name",))
        name = schema.name(data["name"], f"{label}: name")
        label = f"{label} '{name}'"
        if ("image" in data) == ("repository" in data):
            raise ValueError(f"{label}: provide exactly one of image or repository.")
        if "image_tag" in data and "repository" not in data:
            raise ValueError(f"{label}: image_tag is only valid with repository.")
        definition: dict[str, Any] = {
            "essential": schema.boolean(data.get("essential"), f"{label}: essential", default=True)
        }
        for key, camel in (("cpu", "cpu"), ("memory", "memory"), ("memory_reservation", "memoryReservation"),
                           ("stop_timeout", "stopTimeout")):
            value = schema.integer(data.get(key), f"{label}: {key}", minimum=0)
            if value is not None:
                definition[camel] = value
        for key, camel in (("command", "command"), ("entry_point", "entryPoint")):
            if key in data:
                definition[camel] = list(schema.strings(data[key], f"{label}: {key}", allow_empty=False))
        for key, camel in (("working_directory", "workingDirectory"), ("user", "user")):
            value = schema.string(data.get(key), f"{label}: {key}")
            if value is not None:
                definition[camel] = value
        if "readonly_root_filesystem" in data:
            definition["readonlyRootFilesystem"] = schema.boolean(
                data["readonly_root_filesystem"], f"{label}: readonly_root_filesystem"
            )
        ports = []
        for entry in _list(data.get("port_mappings"), f"{label}: port_mappings"):
            where = f"{label}: port_mappings"
            port = schema.mapping(entry, where, {"container_port", "host_port", "protocol", "name", "app_protocol"})
            mapping: dict[str, Any] = {
                "containerPort": _port(port.get("container_port"), f"{where}: container_port", required=True),
                "protocol": _enum(port.get("protocol"), f"{where}: protocol", ("tcp", "udp"), "tcp"),
            }
            if port.get("host_port") is not None:
                mapping["hostPort"] = _port(port["host_port"], f"{where}: host_port")
            if port.get("name") is not None:
                mapping["name"] = schema.name(port["name"], f"{where}: name")
            if port.get("app_protocol") is not None:
                mapping["appProtocol"] = _enum(port["app_protocol"], f"{where}: app_protocol", ("http", "http2", "grpc"))
            ports.append(mapping)
        if ports:
            definition["portMappings"] = ports
        mounts = []
        for entry in _list(data.get("mount_points"), f"{label}: mount_points"):
            where = f"{label}: mount_points"
            mount = schema.mapping(entry, where, {"source_volume", "container_path", "read_only"},
                                   required=("source_volume", "container_path"))
            mounts.append({
                "sourceVolume": schema.required_string(mount["source_volume"], f"{where}: source_volume"),
                "containerPath": schema.required_string(mount["container_path"], f"{where}: container_path"),
                "readOnly": schema.boolean(mount.get("read_only"), f"{where}: read_only"),
            })
        if mounts:
            definition["mountPoints"] = mounts
        if data.get("health_check") is not None:
            where = f"{label}: health_check"
            check = schema.mapping(data["health_check"], where,
                                   {"command", "interval", "timeout", "retries", "start_period"},
                                   required=("command",))
            health: dict[str, Any] = {"command": list(schema.strings(check["command"], f"{where}: command", allow_empty=False))}
            for key, camel in (("interval", "interval"), ("timeout", "timeout"), ("retries", "retries"),
                               ("start_period", "startPeriod")):
                value = schema.integer(check.get(key), f"{where}: {key}", minimum=0)
                if value is not None:
                    health[camel] = value
            definition["healthCheck"] = health
        environment = data.get("environment")
        if isinstance(environment, Mapping):
            pairs = [(key, value) for key, value in environment.items()]
        else:
            pairs = []
            for entry in _list(environment, f"{label}: environment"):
                item = schema.mapping(entry, f"{label}: environment", {"name", "value"}, required=("name",))
                pairs.append((item["name"], item.get("value", "")))
        env = tuple(
            (_env_name(key, f"{label}: environment"),
             str(value).lower() if isinstance(value, bool) else "" if value is None else str(value))
            for key, value in pairs
        )
        if len({key for key, _ in env}) != len(env):
            raise ValueError(f"{label}: duplicate environment variable.")
        log_driver = None
        log_options: dict[str, str] = {}
        if data.get("log_configuration") is not None:
            where = f"{label}: log_configuration"
            log = schema.mapping(data["log_configuration"], where, {"log_driver", "options"}, required=("log_driver",))
            log_driver = schema.required_string(log["log_driver"], f"{where}: log_driver")
            log_options = schema.string_map(log.get("options"), f"{where}: options")
        secrets = ContainerSecret.parse(data.get("secrets"), f"{label}: secrets")
        names = [key for key, _ in env] + [secret.name for secret in secrets]
        if len(names) != len(set(names)):
            raise ValueError(f"{label}: a name is set both as environment and secret, or twice.")
        return cls(
            name=name,
            definition=definition,
            image=schema.string(data.get("image"), f"{label}: image"),
            repository=schema.string(data.get("repository"), f"{label}: repository"),
            image_tag=schema.string(data.get("image_tag"), f"{label}: image_tag", default="latest") or "latest",
            environment=env,
            secrets=secrets,
            log_driver=log_driver,
            log_options=log_options,
        )


def _container_file(raw: str, label: str, *, base_dir: Path | None) -> tuple[Mapping[str, Any], ...]:
    document = load_document(raw, label, base_dir=base_dir)
    if not isinstance(document, list) or not document:
        raise ValueError(f"{label}: must contain a nonempty list of container definitions.")
    names: set[str] = set()
    for index, container in enumerate(document):
        where = f"{label}, container {index + 1}"
        if not isinstance(container, Mapping):
            raise ValueError(f"{where}: must be a mapping.")
        for key in ("name", "image"):
            if not isinstance(container.get(key), str) or not container[key].strip():
                raise ValueError(f"{where}: '{key}' must be a nonempty string.")
        if container["name"] in names:
            raise ValueError(f"{where}: duplicate container name '{container['name']}'.")
        names.add(container["name"])
        for entry in container.get("environment") or []:
            if not isinstance(entry, Mapping):
                raise ValueError(f"{where}: environment entries must be mappings.")
            _env_name(entry.get("name"), where)
        for entry in container.get("secrets") or []:
            if not isinstance(entry, Mapping) or not entry.get("name") or not entry.get("valueFrom"):
                raise ValueError(f"{where}: secrets need name and valueFrom.")
    for name in placeholders(document):
        if name not in FILE_PLACEHOLDERS and not REFERENCE_PLACEHOLDER.fullmatch(name):
            raise ValueError(
                f"{label}: unknown placeholder [[{name}]]. Supported: "
                f"{', '.join(FILE_PLACEHOLDERS)}, repository:<key>, secret:<key>."
            )
    return tuple(json_compatible(document, label))


@dataclass(frozen=True)
class EfsVolumeProps:
    file_system_id: str
    root_directory: str | None = None
    transit_encryption: str = "ENABLED"
    transit_encryption_port: int | None = None
    access_point_id: str | None = None
    iam: str | None = None


@dataclass(frozen=True)
class VolumeProps:
    name: str
    host_path: str | None = None
    docker: Mapping[str, Any] | None = None
    efs: EfsVolumeProps | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> VolumeProps:
        data = schema.mapping(raw, label, {
            "name", "host_path", "docker_volume_configuration", "efs_volume_configuration",
        }, required=("name",))
        name = schema.name(data["name"], f"{label}: name")
        label = f"{label} '{name}'"
        configured = [key for key in ("host_path", "docker_volume_configuration", "efs_volume_configuration") if data.get(key) is not None]
        if len(configured) > 1:
            raise ValueError(f"{label}: choose one of {', '.join(configured)}.")
        docker = None
        if data.get("docker_volume_configuration") is not None:
            where = f"{label}: docker_volume_configuration"
            raw_docker = schema.mapping(data["docker_volume_configuration"], where,
                                        {"scope", "autoprovision", "driver", "driver_opts", "labels"})
            docker = {
                "scope": _enum(raw_docker.get("scope"), f"{where}: scope", ("task", "shared")),
                "autoprovision": (
                    schema.boolean(raw_docker["autoprovision"], f"{where}: autoprovision")
                    if raw_docker.get("autoprovision") is not None else None
                ),
                "driver": schema.string(raw_docker.get("driver"), f"{where}: driver"),
                "driver_opts": schema.string_map(raw_docker.get("driver_opts"), f"{where}: driver_opts") or None,
                "labels": schema.string_map(raw_docker.get("labels"), f"{where}: labels") or None,
            }
            if docker["autoprovision"] is not None and docker["scope"] != "shared":
                raise ValueError(f"{where}: autoprovision requires scope shared.")
        efs = None
        if data.get("efs_volume_configuration") is not None:
            where = f"{label}: efs_volume_configuration"
            raw_efs = schema.mapping(data["efs_volume_configuration"], where, {
                "file_system_id", "root_directory", "transit_encryption",
                "transit_encryption_port", "authorization_config",
            }, required=("file_system_id",))
            auth = schema.mapping(raw_efs.get("authorization_config") or {}, f"{where}: authorization_config",
                                  {"access_point_id", "iam"})
            efs = EfsVolumeProps(
                file_system_id=schema.required_string(raw_efs["file_system_id"], f"{where}: file_system_id"),
                root_directory=schema.string(raw_efs.get("root_directory"), f"{where}: root_directory"),
                transit_encryption=_enum(raw_efs.get("transit_encryption"), f"{where}: transit_encryption",
                                         ("ENABLED", "DISABLED"), "ENABLED") or "ENABLED",
                transit_encryption_port=_port(raw_efs.get("transit_encryption_port"), f"{where}: transit_encryption_port"),
                access_point_id=schema.string(auth.get("access_point_id"), f"{where}: access_point_id"),
                iam=_enum(auth.get("iam"), f"{where}: iam", ("ENABLED", "DISABLED")),
            )
            if (efs.iam == "ENABLED" or efs.access_point_id) and efs.transit_encryption != "ENABLED":
                raise ValueError(f"{where}: IAM authorization and access points need transit_encryption ENABLED.")
            if efs.access_point_id and efs.root_directory not in (None, "/"):
                raise ValueError(f"{where}: root_directory must be '/' when using an access point.")
        return cls(
            name=name,
            host_path=schema.string(data.get("host_path"), f"{label}: host_path"),
            docker=docker,
            efs=efs,
        )


def _placement(raw: Any, label: str, types: tuple[str, ...], value_key: str) -> tuple[tuple[str, str | None], ...]:
    result = []
    for entry in _list(raw, label):
        data = schema.mapping(entry, label, {"type", value_key}, required=("type",))
        result.append((
            _enum(data["type"], f"{label}: type", types) or "",
            schema.string(data.get(value_key), f"{label}: {value_key}"),
        ))
    return tuple(result)


@dataclass(frozen=True)
class TaskDefinitionProps:
    family: str
    containers: tuple[ContainerProps, ...] = ()
    container_file: tuple[Mapping[str, Any], ...] = ()
    task_role: str | None = None
    task_role_arn: str | None = None
    execution_role: str | None = None
    execution_role_arn: str | None = None
    network_mode: str = "bridge"
    requires_compatibilities: tuple[str, ...] = ("EC2",)
    cpu: str | None = None
    memory: str | None = None
    database_secret: str | None = None
    smtp_secret: str | None = None
    backend_url: str | None = None
    backend_load_balancer: str | None = None
    backend_load_balancer_port: int | None = None
    frontend_url: str | None = None
    frontend_load_balancer: str | None = None
    cloud_map_service: str | None = None
    cloud_map_port: int | None = None
    log_retention_days: int = 30
    volumes: tuple[VolumeProps, ...] = ()
    placement_constraints: tuple[tuple[str, str | None], ...] = ()
    proxy_container: str | None = None
    proxy_type: str = "APPMESH"
    proxy_properties: Mapping[str, str] = field(default_factory=dict)
    operating_system_family: str | None = None
    cpu_architecture: str | None = None

    @property
    def container_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.containers) or tuple(c["name"] for c in self.container_file)

    @property
    def port_mappings(self) -> dict[str, set[int]]:
        result: dict[str, set[int]] = {}
        for container in self.containers:
            result[container.name] = {p["containerPort"] for p in container.definition.get("portMappings", [])}
        for container in self.container_file:
            result[container["name"]] = {
                p.get("containerPort") for p in container.get("portMappings") or [] if isinstance(p, Mapping)
            }
        return result

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> TaskDefinitionProps:
        data = schema.mapping(raw, "ECS task definition", {
            "family", "task_role", "task_role_arn", "execution_role", "execution_role_arn",
            "network_mode", "requires_compatibilities", "cpu", "memory",
            "container_definitions", "container_definitions_file", "database_secret",
            "smtp_secret", "backend_url", "backend_load_balancer", "backend_load_balancer_port",
            "frontend_url", "frontend_load_balancer", "cloud_map_service", "cloud_map_port",
            "log_retention_days", "volumes", "placement_constraints", "proxy_configuration",
            "runtime_platform",
        }, required=("family",))
        family = schema.name(data["family"], "ECS task definition family")
        label = f"ECS task definition '{family}'"
        for key in ("task_role", "execution_role"):
            if key in data and f"{key}_arn" in data:
                raise ValueError(f"{label}: provide only one of {key} or {key}_arn.")
            arn = data.get(f"{key}_arn")
            if arn is not None and (not isinstance(arn, str) or not ROLE_ARN.fullmatch(arn)):
                raise ValueError(f"{label}: {key}_arn must be an IAM role ARN.")
        if ("container_definitions" in data) == ("container_definitions_file" in data):
            raise ValueError(f"{label}: provide exactly one of container_definitions or container_definitions_file.")
        containers: tuple[ContainerProps, ...] = ()
        container_file: tuple[Mapping[str, Any], ...] = ()
        if "container_definitions" in data:
            containers = schema.items(
                data["container_definitions"], f"{label}: container_definitions",
                lambda item: ContainerProps.from_dict(item, f"{label}, container"),
            )
            if not containers:
                raise ValueError(f"{label}: container_definitions must not be empty.")
        else:
            path = schema.required_string(data["container_definitions_file"], f"{label}: container_definitions_file")
            container_file = _container_file(path, f"{label}, container_definitions_file", base_dir=base_dir)
        network_mode = _enum(data.get("network_mode"), f"{label}: network_mode", NETWORK_MODES, "bridge") or "bridge"
        compat = schema.strings(data.get("requires_compatibilities"), f"{label}: requires_compatibilities") or ("EC2",)
        for item in compat:
            _enum(item, f"{label}: requires_compatibilities", COMPATIBILITIES)
        cpu = _number_string(data.get("cpu"), f"{label}: cpu")
        memory = _number_string(data.get("memory"), f"{label}: memory")
        if "FARGATE" in compat and (network_mode != "awsvpc" or cpu is None or memory is None):
            raise ValueError(f"{label}: FARGATE tasks need network_mode awsvpc, cpu and memory.")
        backends = [key for key in ("backend_url", "backend_load_balancer", "cloud_map_service") if data.get(key) is not None]
        if len(backends) > 1:
            raise ValueError(f"{label}: choose one of {', '.join(backends)}.")
        if data.get("frontend_url") is not None and data.get("frontend_load_balancer") is not None:
            raise ValueError(f"{label}: choose one of frontend_url or frontend_load_balancer.")
        if data.get("backend_load_balancer_port") is not None and data.get("backend_load_balancer") is None:
            raise ValueError(f"{label}: backend_load_balancer_port needs backend_load_balancer.")
        if data.get("cloud_map_port") is not None and data.get("cloud_map_service") is None:
            raise ValueError(f"{label}: cloud_map_port needs cloud_map_service.")
        cloud_map_service = schema.string(data.get("cloud_map_service"), f"{label}: cloud_map_service")
        if cloud_map_service is not None and cloud_map_service.count("/") != 1:
            raise ValueError(f"{label}: cloud_map_service must be '<namespace>/<service>'.")
        volumes = schema.items(data.get("volumes"), f"{label}: volumes",
                               lambda item: VolumeProps.from_dict(item, f"{label}: volume"))
        proxy = schema.mapping(data.get("proxy_configuration") or {}, f"{label}: proxy_configuration",
                               {"container_name", "type", "properties"})
        if proxy and "container_name" not in proxy:
            raise ValueError(f"{label}: proxy_configuration needs container_name.")
        platform = schema.mapping(data.get("runtime_platform") or {}, f"{label}: runtime_platform",
                                  {"operating_system_family", "cpu_architecture"})
        props = cls(
            family=family,
            containers=containers,
            container_file=container_file,
            task_role=schema.string(data.get("task_role"), f"{label}: task_role"),
            task_role_arn=data.get("task_role_arn"),
            execution_role=schema.string(data.get("execution_role"), f"{label}: execution_role"),
            execution_role_arn=data.get("execution_role_arn"),
            network_mode=network_mode,
            requires_compatibilities=compat,
            cpu=cpu,
            memory=memory,
            database_secret=schema.string(data.get("database_secret"), f"{label}: database_secret"),
            smtp_secret=schema.string(data.get("smtp_secret"), f"{label}: smtp_secret"),
            backend_url=schema.string(data.get("backend_url"), f"{label}: backend_url"),
            backend_load_balancer=schema.string(data.get("backend_load_balancer"), f"{label}: backend_load_balancer"),
            backend_load_balancer_port=_port(data.get("backend_load_balancer_port"), f"{label}: backend_load_balancer_port"),
            frontend_url=schema.string(data.get("frontend_url"), f"{label}: frontend_url"),
            frontend_load_balancer=schema.string(data.get("frontend_load_balancer"), f"{label}: frontend_load_balancer"),
            cloud_map_service=cloud_map_service,
            cloud_map_port=_port(data.get("cloud_map_port"), f"{label}: cloud_map_port"),
            log_retention_days=schema.integer(data.get("log_retention_days"), f"{label}: log_retention_days",
                                              default=30, minimum=1) or 30,
            volumes=volumes,
            placement_constraints=_placement(data.get("placement_constraints"), f"{label}: placement_constraints",
                                             ("memberOf",), "expression"),
            proxy_container=schema.string(proxy.get("container_name"), f"{label}: proxy container_name"),
            proxy_type=_enum(proxy.get("type"), f"{label}: proxy type", ("APPMESH",), "APPMESH") or "APPMESH",
            proxy_properties=schema.string_map(proxy.get("properties"), f"{label}: proxy properties"),
            operating_system_family=schema.string(platform.get("operating_system_family"), f"{label}: operating_system_family",
                                                  default="LINUX" if platform else None),
            cpu_architecture=_enum(platform.get("cpu_architecture"), f"{label}: cpu_architecture", ("X86_64", "ARM64"),
                                   "X86_64" if platform else None),
        )
        props._check_file_tokens(label)
        if props.log_retention_days not in LOG_RETENTION_DAYS:
            raise ValueError(
                f"{label}: log_retention_days must be one of {', '.join(map(str, LOG_RETENTION_DAYS))}."
            )
        volume_names = {volume.name for volume in volumes}
        for container in containers:
            for mount in container.definition.get("mountPoints", []):
                if mount["sourceVolume"] not in volume_names:
                    raise ValueError(f"{label}: container '{container.name}' mounts unknown volume '{mount['sourceVolume']}'.")
        if props.proxy_container and props.proxy_container not in props.container_names:
            raise ValueError(f"{label}: proxy container '{props.proxy_container}' is not defined.")
        return props

    def _check_file_tokens(self, label: str) -> None:
        used = {token for text in _strings_in(list(self.container_file)) for token in LEGACY_TOKENS if token in text}
        needs = {
            "__BACKEND_URL__": self.backend_url or self.backend_load_balancer or self.cloud_map_service,
            "__SECRET_ARN__": self.database_secret,
            "__FRONTEND_URL__": self.frontend_url or self.frontend_load_balancer,
            "__SMTP_SECRET_ARN__": self.smtp_secret,
        }
        for token in sorted(used):
            if needs[token] is None:
                raise ValueError(f"{label}: container_definitions_file uses {token} but nothing configures it.")


@dataclass(frozen=True)
class RegistryProps:
    cloud_map_service: str | None = None
    registry_arn: str | None = None
    port: int | None = None
    container_name: str | None = None
    container_port: int | None = None


@dataclass(frozen=True)
class ServiceProps:
    name: str
    task_definition: str | None = None
    task_definition_arn: str | None = None
    desired_count: int | None = 1
    launch_type: str | None = "EC2"
    platform_version: str | None = None
    scheduling_strategy: str = "REPLICA"
    maximum_percent: int = 200
    minimum_healthy_percent: int = 100
    enable_ecs_managed_tags: bool = False
    enable_execute_command: bool = False
    health_check_grace_period_seconds: int | None = None
    propagate_tags: str | None = None
    capacity_provider_strategy: tuple[StrategyItem, ...] = ()
    circuit_breaker: tuple[bool, bool] | None = None
    deployment_controller: str | None = None
    load_balancers: tuple[tuple[str, str, int], ...] = ()
    subnets: str | None = None
    security_groups: tuple[str, ...] = ()
    assign_public_ip: bool = False
    placement_constraints: tuple[tuple[str, str | None], ...] = ()
    placement_strategies: tuple[tuple[str, str | None], ...] = ()
    registry: RegistryProps | None = None

    @property
    def uses_fargate(self) -> bool:
        if self.capacity_provider_strategy:
            return bool(strategy_is_fargate(tuple(i.capacity_provider for i in self.capacity_provider_strategy)))
        return self.launch_type == "FARGATE"

    @classmethod
    def from_dict(cls, raw: Any) -> ServiceProps:
        data = schema.mapping(raw, "ECS service", {
            "name", "task_definition", "task_definition_arn", "desired_count", "launch_type",
            "platform_version", "scheduling_strategy", "deployment_maximum_percent",
            "deployment_minimum_healthy_percent", "enable_ecs_managed_tags",
            "enable_execute_command", "health_check_grace_period_seconds", "propagate_tags",
            "capacity_provider_strategy", "deployment_circuit_breaker", "deployment_controller",
            "load_balancers", "network_configuration", "placement_constraints",
            "ordered_placement_strategy", "service_registries",
        }, required=("name",))
        name = schema.name(data["name"], "ECS service name")
        label = f"ECS service '{name}'"
        if ("task_definition" in data) == ("task_definition_arn" in data):
            raise ValueError(f"{label}: provide exactly one of task_definition or task_definition_arn.")
        arn = data.get("task_definition_arn")
        if arn is not None and (not isinstance(arn, str) or not TASK_DEFINITION_ARN.fullmatch(arn)):
            raise ValueError(f"{label}: task_definition_arn must be an ECS task definition ARN.")
        strategy = StrategyItem.parse(data.get("capacity_provider_strategy"), f"{label}: capacity_provider_strategy")
        if strategy and data.get("launch_type") is not None:
            raise ValueError(f"{label}: launch_type and capacity_provider_strategy are mutually exclusive.")
        launch_type = None if strategy else _enum(data.get("launch_type"), f"{label}: launch_type", LAUNCH_TYPES, "EC2")
        scheduling = _enum(data.get("scheduling_strategy"), f"{label}: scheduling_strategy", SCHEDULING, "REPLICA") or "REPLICA"
        desired = schema.integer(data.get("desired_count"), f"{label}: desired_count", default=1, minimum=0)
        if scheduling == "DAEMON":
            if "desired_count" in data:
                raise ValueError(f"{label}: DAEMON services do not take desired_count.")
            desired = None
        breaker = None
        if data.get("deployment_circuit_breaker") is not None:
            where = f"{label}: deployment_circuit_breaker"
            raw_breaker = schema.mapping(data["deployment_circuit_breaker"], where, {"enable", "rollback"},
                                         required=("enable", "rollback"))
            breaker = (schema.boolean(raw_breaker["enable"], where), schema.boolean(raw_breaker["rollback"], where))
        controller = None
        if data.get("deployment_controller") is not None:
            raw_controller = schema.mapping(data["deployment_controller"], f"{label}: deployment_controller", {"type"})
            controller = _enum(raw_controller.get("type"), f"{label}: deployment_controller type", CONTROLLERS, "ECS")
        load_balancers = []
        for entry in _list(data.get("load_balancers"), f"{label}: load_balancers"):
            where = f"{label}: load_balancers"
            lb = schema.mapping(entry, where, {"target_group", "container_name", "container_port"},
                                required=("target_group", "container_name", "container_port"))
            load_balancers.append((
                schema.required_string(lb["target_group"], f"{where}: target_group"),
                schema.required_string(lb["container_name"], f"{where}: container_name"),
                _port(lb["container_port"], f"{where}: container_port", required=True) or 0,
            ))
        grace = schema.integer(data.get("health_check_grace_period_seconds"),
                               f"{label}: health_check_grace_period_seconds", minimum=0)
        network = None
        if data.get("network_configuration") is not None:
            where = f"{label}: network_configuration"
            network = schema.mapping(data["network_configuration"], where,
                                     {"subnets", "security_groups", "assign_public_ip"},
                                     required=("security_groups",))
        registry = None
        if data.get("service_registries") is not None:
            where = f"{label}: service_registries"
            raw_registry = schema.mapping(data["service_registries"], where, {
                "cloud_map_service", "registry_arn", "port", "container_name", "container_port",
            })
            if ("cloud_map_service" in raw_registry) == ("registry_arn" in raw_registry):
                raise ValueError(f"{where}: provide exactly one of cloud_map_service or registry_arn.")
            registry = RegistryProps(
                cloud_map_service=schema.string(raw_registry.get("cloud_map_service"), f"{where}: cloud_map_service"),
                registry_arn=schema.string(raw_registry.get("registry_arn"), f"{where}: registry_arn"),
                port=_port(raw_registry.get("port"), f"{where}: port"),
                container_name=schema.string(raw_registry.get("container_name"), f"{where}: container_name"),
                container_port=_port(raw_registry.get("container_port"), f"{where}: container_port"),
            )
        props = cls(
            name=name,
            task_definition=schema.string(data.get("task_definition"), f"{label}: task_definition"),
            task_definition_arn=arn,
            desired_count=desired,
            launch_type=launch_type,
            platform_version=schema.string(data.get("platform_version"), f"{label}: platform_version"),
            scheduling_strategy=scheduling,
            maximum_percent=schema.integer(data.get("deployment_maximum_percent"), f"{label}: deployment_maximum_percent",
                                           default=200, minimum=0) or 0,
            minimum_healthy_percent=schema.integer(data.get("deployment_minimum_healthy_percent"),
                                                   f"{label}: deployment_minimum_healthy_percent",
                                                   default=100, minimum=0, maximum=100) or 0,
            enable_ecs_managed_tags=schema.boolean(data.get("enable_ecs_managed_tags"), f"{label}: enable_ecs_managed_tags"),
            enable_execute_command=schema.boolean(data.get("enable_execute_command"), f"{label}: enable_execute_command"),
            health_check_grace_period_seconds=grace,
            propagate_tags=_enum(data.get("propagate_tags"), f"{label}: propagate_tags", PROPAGATE_TAGS),
            capacity_provider_strategy=strategy,
            circuit_breaker=breaker,
            deployment_controller=controller,
            load_balancers=tuple(load_balancers),
            subnets=_enum((network or {}).get("subnets"), f"{label}: network_configuration subnets", TIERS,
                          "private" if network is not None else None),
            security_groups=(
                schema.strings(network["security_groups"], f"{label}: network_configuration security_groups",
                               allow_empty=False)
                if network is not None else ()
            ),
            assign_public_ip=schema.boolean((network or {}).get("assign_public_ip"), f"{label}: assign_public_ip"),
            placement_constraints=_placement(data.get("placement_constraints"), f"{label}: placement_constraints",
                                             ("distinctInstance", "memberOf"), "expression"),
            placement_strategies=_placement(data.get("ordered_placement_strategy"), f"{label}: ordered_placement_strategy",
                                            ("random", "spread", "binpack"), "field"),
            registry=registry,
        )
        if props.assign_public_ip and not props.uses_fargate:
            raise ValueError(f"{label}: assign_public_ip is only supported for Fargate tasks.")
        if scheduling == "DAEMON" and props.uses_fargate:
            raise ValueError(f"{label}: Fargate does not support DAEMON scheduling.")
        return props


@dataclass(frozen=True)
class DiscoveryServiceProps:
    name: str
    has_dns_config: bool = False
    routing_policy: str = "MULTIVALUE"
    dns_records: tuple[tuple[str, int], ...] = ()
    health_check: tuple[str, str | None, int] | None = None
    custom_health_check_threshold: int | None = None


@dataclass(frozen=True)
class NamespaceProps:
    name: str
    type: str = "DNS_PRIVATE"
    description: str | None = None
    services: tuple[DiscoveryServiceProps, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any) -> NamespaceProps:
        data = schema.mapping(raw, "Cloud Map namespace", {"name", "type", "description", "services"},
                              required=("name",))
        name = data["name"]
        if not isinstance(name, str) or not DNS_NAME.fullmatch(name):
            raise ValueError("Cloud Map namespace name must be a DNS-style name such as app.local.")
        label = f"Cloud Map namespace '{name}'"
        kind = _enum(data.get("type"), f"{label}: type", NAMESPACE_TYPES, "DNS_PRIVATE") or "DNS_PRIVATE"

        def service(entry: Any) -> DiscoveryServiceProps:
            svc = schema.mapping(entry, f"{label}: service", {
                "name", "dns_config", "health_check_config", "health_check_custom_config",
            }, required=("name",))
            svc_name = schema.name(svc["name"], f"{label}: service name")
            where = f"{label}, service '{svc_name}'"
            dns = svc.get("dns_config")
            records: list[tuple[str, int]] = []
            routing = "MULTIVALUE"
            if dns is not None:
                if kind == "HTTP":
                    raise ValueError(f"{where}: HTTP namespaces do not support dns_config.")
                dns = schema.mapping(dns, f"{where}: dns_config", {"routing_policy", "dns_records"})
                routing = _enum(dns.get("routing_policy"), f"{where}: routing_policy", ("MULTIVALUE", "WEIGHTED"),
                                "MULTIVALUE") or "MULTIVALUE"
                for record in _list(dns.get("dns_records"), f"{where}: dns_records"):
                    record = schema.mapping(record, f"{where}: dns_records", {"type", "ttl"}, required=("type", "ttl"))
                    records.append((
                        _enum(record["type"], f"{where}: dns record type", DNS_RECORD_TYPES) or "",
                        schema.integer(record["ttl"], f"{where}: ttl", minimum=0) or 0,
                    ))
                if not records:
                    raise ValueError(f"{where}: dns_config needs at least one dns_records entry.")
            check = None
            if svc.get("health_check_config") is not None:
                if kind != "DNS_PUBLIC":
                    raise ValueError(f"{where}: health_check_config is only valid in DNS_PUBLIC namespaces.")
                hc = schema.mapping(svc["health_check_config"], f"{where}: health_check_config",
                                    {"type", "resource_path", "failure_threshold"}, required=("type",))
                check = (
                    _enum(hc["type"], f"{where}: health check type", ("HTTP", "HTTPS", "TCP")) or "",
                    schema.string(hc.get("resource_path"), f"{where}: resource_path"),
                    schema.integer(hc.get("failure_threshold"), f"{where}: failure_threshold", default=1,
                                   minimum=1, maximum=10) or 1,
                )
            custom = None
            if svc.get("health_check_custom_config") is not None:
                cc = schema.mapping(svc["health_check_custom_config"], f"{where}: health_check_custom_config",
                                    {"failure_threshold"})
                custom = schema.integer(cc.get("failure_threshold"), f"{where}: failure_threshold", default=1,
                                        minimum=1, maximum=10)
            if check is not None and custom is not None:
                raise ValueError(f"{where}: choose health_check_config or health_check_custom_config.")
            return DiscoveryServiceProps(svc_name, dns is not None, routing, tuple(records), check, custom)

        return cls(
            name=name,
            type=kind,
            description=schema.string(data.get("description"), f"{label}: description"),
            services=schema.items(data.get("services"), f"{label}: services", service),
        )


@dataclass(frozen=True)
class LaunchTemplateProps:
    name: str
    instance_type: str
    ami: str | None = "ECSAL2023"
    ami_id: str | None = None
    ami_ssm_parameter: str | None = None
    instance_profile: str | None = None
    key_pair: str | None = None
    associate_public_ip_address: bool | None = None
    security_groups: tuple[str, ...] = ()
    tags: Mapping[str, str] = field(default_factory=dict)
    user_data: str | None = None
    volume_size: int | None = None
    root_device_name: str = "/dev/xvda"
    require_imdsv2: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> LaunchTemplateProps:
        data = schema.mapping(raw, "ECS launch template", {
            "name", "instance_type", "ami", "ami_id", "ami_ssm_parameter", "instance_profile",
            "key_pair", "associate_public_ip_address", "security_groups", "tags", "user_data",
            "volume_size", "root_device_name", "require_imdsv2",
        }, required=("name", "instance_type"))
        name = schema.name(data["name"], "ECS launch template name")
        label = f"ECS launch template '{name}'"
        sources = [key for key in ("ami", "ami_id", "ami_ssm_parameter") if key in data]
        if len(sources) > 1:
            raise ValueError(f"{label}: choose one of ami, ami_id or ami_ssm_parameter.")
        ami_id = schema.string(data.get("ami_id"), f"{label}: ami_id")
        if ami_id is not None and not re.fullmatch(r"ami-[0-9a-f]{8,17}", ami_id):
            raise ValueError(f"{label}: ami_id must look like ami-0123456789abcdef0.")
        parameter = schema.string(data.get("ami_ssm_parameter"), f"{label}: ami_ssm_parameter")
        if parameter is not None and not parameter.startswith("/"):
            raise ValueError(f"{label}: ami_ssm_parameter must be an SSM parameter path.")
        ami = None if sources and sources[0] != "ami" else _enum(
            data.get("ami"), f"{label}: ami", tuple(AMIS), "ECSAL2023"
        )
        public_ip = data.get("associate_public_ip_address")
        return cls(
            name=name,
            instance_type=schema.required_string(data["instance_type"], f"{label}: instance_type"),
            ami=ami,
            ami_id=ami_id,
            ami_ssm_parameter=parameter,
            instance_profile=schema.string(data.get("instance_profile"), f"{label}: instance_profile"),
            key_pair=schema.string(data.get("key_pair"), f"{label}: key_pair"),
            associate_public_ip_address=(
                schema.boolean(public_ip, f"{label}: associate_public_ip_address") if public_ip is not None else None
            ),
            security_groups=schema.strings(data.get("security_groups"), f"{label}: security_groups"),
            tags=schema.string_map(data.get("tags"), f"{label}: tags"),
            user_data=schema.string(data.get("user_data"), f"{label}: user_data"),
            volume_size=schema.integer(data.get("volume_size"), f"{label}: volume_size", minimum=1, maximum=16384),
            root_device_name=schema.string(data.get("root_device_name"), f"{label}: root_device_name",
                                           default="/dev/xvda") or "/dev/xvda",
            require_imdsv2=schema.boolean(data.get("require_imdsv2"), f"{label}: require_imdsv2", default=True),
        )


@dataclass(frozen=True)
class AutoScalingGroupProps:
    name: str
    min_size: int
    max_size: int
    desired_capacity: int | None = None
    health_check_type: str | None = None
    health_check_grace_period: int | None = None
    subnets: str = "private"
    target_groups: tuple[str, ...] = ()
    launch_template: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)
    additional_tags: tuple[tuple[str, str, bool], ...] = ()

    @classmethod
    def from_dict(cls, raw: Any) -> AutoScalingGroupProps:
        data = schema.mapping(raw, "ECS autoscaling_group", {
            "name", "min_size", "max_size", "desired_capacity", "health_check_type",
            "health_check_grace_period", "subnets", "target_groups", "launch_template",
            "tags", "additional_tags",
        }, required=("name", "min_size", "max_size"))
        name = schema.name(data["name"], "ECS autoscaling_group name")
        label = f"ECS autoscaling group '{name}'"
        min_size = schema.integer(data["min_size"], f"{label}: min_size", minimum=0) or 0
        max_size = schema.integer(data["max_size"], f"{label}: max_size", minimum=1) or 0
        desired = schema.integer(data.get("desired_capacity"), f"{label}: desired_capacity", minimum=0)
        if min_size > max_size or (desired is not None and not min_size <= desired <= max_size):
            raise ValueError(f"{label}: need min_size <= desired_capacity <= max_size.")
        additional = []
        for entry in _list(data.get("additional_tags"), f"{label}: additional_tags"):
            tag = schema.mapping(entry, f"{label}: additional_tags", {"key", "value", "propagate_at_launch"},
                                 required=("key",))
            additional.append((
                schema.required_string(tag["key"], f"{label}: tag key"),
                str(tag.get("value", "")),
                schema.boolean(tag.get("propagate_at_launch"), f"{label}: propagate_at_launch", default=True),
            ))
        return cls(
            name=name,
            min_size=min_size,
            max_size=max_size,
            desired_capacity=desired,
            health_check_type=_enum(data.get("health_check_type"), f"{label}: health_check_type", ("EC2", "ELB")),
            health_check_grace_period=schema.integer(data.get("health_check_grace_period"),
                                                     f"{label}: health_check_grace_period", minimum=0),
            subnets=_enum(data.get("subnets"), f"{label}: subnets", TIERS, "private") or "private",
            target_groups=schema.strings(data.get("target_groups"), f"{label}: target_groups"),
            launch_template=schema.string(data.get("launch_template"), f"{label}: launch_template"),
            tags=schema.string_map(data.get("tags"), f"{label}: tags"),
            additional_tags=tuple(additional),
        )


@dataclass(frozen=True)
class CapacityProviderProps:
    name: str
    managed_termination_protection: str = "DISABLED"
    maximum_scaling_step_size: int = 10
    minimum_scaling_step_size: int = 1
    status: str = "ENABLED"
    target_capacity: int = 100
    instance_warmup_period: int = 300

    @classmethod
    def from_dict(cls, raw: Any) -> CapacityProviderProps:
        data = schema.mapping(raw, "ECS capacity_provider", {"name", "managed_termination_protection", "managed_scaling"},
                              required=("name",))
        name = schema.name(data["name"], "ECS capacity_provider name")
        label = f"ECS capacity provider '{name}'"
        if name.upper() in FARGATE_PROVIDERS:
            raise ValueError(f"{label}: FARGATE names are reserved.")
        scaling = schema.mapping(data.get("managed_scaling") or {}, f"{label}: managed_scaling", {
            "maximum_scaling_step_size", "minimum_scaling_step_size", "status",
            "target_capacity", "instance_warmup_period",
        })
        props = cls(
            name=name,
            managed_termination_protection=_enum(
                data.get("managed_termination_protection"), f"{label}: managed_termination_protection",
                ("ENABLED", "DISABLED"), "DISABLED") or "DISABLED",
            maximum_scaling_step_size=schema.integer(scaling.get("maximum_scaling_step_size"), f"{label}: maximum_scaling_step_size",
                                                     default=10, minimum=1, maximum=10000) or 10,
            minimum_scaling_step_size=schema.integer(scaling.get("minimum_scaling_step_size"), f"{label}: minimum_scaling_step_size",
                                                     default=1, minimum=1, maximum=10000) or 1,
            status=_enum(scaling.get("status"), f"{label}: status", ("ENABLED", "DISABLED"), "ENABLED") or "ENABLED",
            target_capacity=schema.integer(scaling.get("target_capacity"), f"{label}: target_capacity",
                                           default=100, minimum=1, maximum=100) or 100,
            instance_warmup_period=schema.integer(scaling.get("instance_warmup_period"), f"{label}: instance_warmup_period",
                                                  default=300, minimum=0, maximum=10000) or 0,
        )
        if props.minimum_scaling_step_size > props.maximum_scaling_step_size:
            raise ValueError(f"{label}: minimum_scaling_step_size exceeds maximum_scaling_step_size.")
        if props.managed_termination_protection == "ENABLED" and props.status != "ENABLED":
            raise ValueError(f"{label}: managed termination protection needs managed scaling ENABLED.")
        return props


@dataclass(frozen=True)
class ScalingPolicyProps:
    scaling_adjustment: int
    adjustment_type: str = "ChangeInCapacity"
    cooldown: int = 300

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> ScalingPolicyProps:
        data = schema.mapping(raw, label, {"scaling_adjustment", "adjustment_type", "cooldown"},
                              required=("scaling_adjustment",))
        return cls(
            scaling_adjustment=schema.integer(data["scaling_adjustment"], f"{label}: scaling_adjustment") or 0,
            adjustment_type=_enum(data.get("adjustment_type"), f"{label}: adjustment_type",
                                  ("ChangeInCapacity", "ExactCapacity", "PercentChangeInCapacity"),
                                  "ChangeInCapacity") or "ChangeInCapacity",
            cooldown=schema.integer(data.get("cooldown"), f"{label}: cooldown", default=300, minimum=0) or 0,
        )


@dataclass(frozen=True)
class Ec2AutoscalingProps:
    launch_templates: tuple[LaunchTemplateProps, ...]
    autoscaling_group: AutoScalingGroupProps
    capacity_provider: CapacityProviderProps
    scale_up: ScalingPolicyProps | None = None
    scale_down: ScalingPolicyProps | None = None

    @property
    def launch_template(self) -> LaunchTemplateProps:
        wanted = self.autoscaling_group.launch_template
        return next(lt for lt in self.launch_templates if wanted is None or lt.name == wanted)

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> Ec2AutoscalingProps:
        data = schema.mapping(raw, label, {"launch_templates", "autoscaling_group", "capacity_provider", "scaling_policies"},
                              required=("launch_templates", "autoscaling_group", "capacity_provider"))
        templates = schema.items(data["launch_templates"], f"{label}: launch_templates", LaunchTemplateProps.from_dict)
        if not templates:
            raise ValueError(f"{label}: launch_templates must not be empty.")
        group = AutoScalingGroupProps.from_dict(data["autoscaling_group"])
        if group.launch_template is None and len(templates) > 1:
            raise ValueError(f"{label}: autoscaling_group.launch_template is required with several launch templates.")
        if group.launch_template is not None:
            schema.lookup({lt.name: lt for lt in templates}, group.launch_template, f"{label}: autoscaling_group.launch_template")
        scale_up = scale_down = None
        if data.get("scaling_policies") is not None:
            policies = schema.mapping(data["scaling_policies"], f"{label}: scaling_policies", {"scale_up", "scale_down"},
                                      required=("scale_up", "scale_down"))
            scale_up = ScalingPolicyProps.from_dict(policies["scale_up"], f"{label}: scale_up")
            scale_down = ScalingPolicyProps.from_dict(policies["scale_down"], f"{label}: scale_down")
        return cls(templates, group, CapacityProviderProps.from_dict(data["capacity_provider"]), scale_up, scale_down)


@dataclass(frozen=True)
class EcsClusterProps:
    name: str
    create: bool = True
    container_insights: bool = False
    execute_command: ExecuteCommandProps | None = None
    capacity_provider_names: tuple[str, ...] = ()
    default_strategy: tuple[StrategyItem, ...] = ()
    has_capacity_providers: bool = False
    task_definitions: tuple[TaskDefinitionProps, ...] = ()
    services: tuple[ServiceProps, ...] = ()
    namespaces: tuple[NamespaceProps, ...] = ()
    ec2_autoscaling: Ec2AutoscalingProps | None = None

    @property
    def discovery_keys(self) -> set[str]:
        return {f"{ns.name}/{svc.name}" for ns in self.namespaces for svc in ns.services}

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EcsClusterProps:
        data = schema.mapping(raw, "ECS cluster", {
            "name", "create", "container_insights", "execute_command_configuration",
            "capacity_providers", "task_definitions", "services", "cloud_map_namespaces",
            "ec2_autoscaling",
        }, required=("name",))
        name = schema.name(data["name"], "ECS cluster name")
        label = f"ECS cluster '{name}'"
        names: tuple[str, ...] = ()
        strategy: tuple[StrategyItem, ...] = ()
        if data.get("capacity_providers") is not None:
            providers = schema.mapping(data["capacity_providers"], f"{label}: capacity_providers",
                                       {"names", "default_strategy"}, required=("names",))
            names = schema.strings(providers["names"], f"{label}: capacity_providers.names", allow_empty=False)
            if len(set(names)) != len(names):
                raise ValueError(f"{label}: duplicate capacity provider name.")
            strategy = StrategyItem.parse(providers.get("default_strategy"), f"{label}: default_strategy")
        props = cls(
            name=name,
            create=schema.boolean(data.get("create"), f"{label}: create", default=True),
            container_insights=schema.boolean(data.get("container_insights"), f"{label}: container_insights"),
            execute_command=(
                ExecuteCommandProps.from_dict(data["execute_command_configuration"], f"{label}: execute_command_configuration")
                if data.get("execute_command_configuration") is not None else None
            ),
            capacity_provider_names=names,
            default_strategy=strategy,
            has_capacity_providers=data.get("capacity_providers") is not None,
            task_definitions=schema.items(
                data.get("task_definitions"), f"{label}: task_definitions",
                lambda item: TaskDefinitionProps.from_dict(item, base_dir=base_dir),
                key=lambda item: item.family,
            ),
            services=schema.items(data.get("services"), f"{label}: services", ServiceProps.from_dict),
            namespaces=schema.items(data.get("cloud_map_namespaces"), f"{label}: cloud_map_namespaces",
                                    NamespaceProps.from_dict),
            ec2_autoscaling=(
                Ec2AutoscalingProps.from_dict(data["ec2_autoscaling"], f"{label}: ec2_autoscaling")
                if data.get("ec2_autoscaling") is not None else None
            ),
        )
        props._validate(label)
        return props

    def _validate(self, label: str) -> None:
        ec2_provider = self.ec2_autoscaling.capacity_provider.name if self.ec2_autoscaling else None
        known = set(FARGATE_PROVIDERS) | ({ec2_provider} if ec2_provider else set())
        for provider in self.capacity_provider_names:
            if provider not in known:
                raise ValueError(
                    f"{label}: unknown capacity provider '{provider}'. Use FARGATE, FARGATE_SPOT"
                    + (f" or '{ec2_provider}'." if ec2_provider else " or configure ec2_autoscaling.")
                )
        for item in self.default_strategy:
            if item.capacity_provider not in self.capacity_provider_names:
                raise ValueError(f"{label}: default strategy provider '{item.capacity_provider}' is not in capacity_providers.names.")
        families = {td.family: td for td in self.task_definitions}
        for service in self.services:
            where = f"{label}, service '{service.name}'"
            for item in service.capacity_provider_strategy:
                if item.capacity_provider not in self.capacity_provider_names:
                    raise ValueError(f"{where}: capacity provider '{item.capacity_provider}' is not associated with the cluster.")
            if service.registry and service.registry.cloud_map_service:
                schema.lookup({key: key for key in self.discovery_keys}, service.registry.cloud_map_service,
                              f"{where}: service_registries.cloud_map_service")
            if service.task_definition is None:
                continue
            task = schema.lookup(families, service.task_definition, f"{where}: task_definition")
            if (task.network_mode == "awsvpc") != bool(service.security_groups):
                raise ValueError(f"{where}: network_configuration is required with, and only with, awsvpc tasks.")
            if service.uses_fargate and "FARGATE" not in task.requires_compatibilities:
                raise ValueError(f"{where}: Fargate services need a task definition compatible with FARGATE.")
            ports = task.port_mappings
            for _, container, port in service.load_balancers:
                if container not in ports:
                    raise ValueError(f"{where}: load balancer container '{container}' is not in task '{task.family}'.")
                if port not in ports[container]:
                    raise ValueError(f"{where}: container '{container}' does not map port {port}.")


@dataclass(frozen=True)
class EcsProps:
    clusters: tuple[EcsClusterProps, ...] = ()

    def __post_init__(self) -> None:
        clusters = [cluster for cluster in self.clusters if cluster.create]
        for kind, values in (
            ("task definition family", [td.family for c in clusters for td in c.task_definitions]),
            ("service", [svc.name for c in clusters for svc in c.services]),
            ("Cloud Map namespace", [ns.name for c in clusters for ns in c.namespaces]),
            ("launch template", [lt.name for c in clusters if c.ec2_autoscaling for lt in c.ec2_autoscaling.launch_templates]),
            ("autoscaling group", [c.ec2_autoscaling.autoscaling_group.name for c in clusters if c.ec2_autoscaling]),
            ("capacity provider", [c.ec2_autoscaling.capacity_provider.name for c in clusters if c.ec2_autoscaling]),
        ):
            folded = [value.casefold() for value in values]
            if len(folded) != len(set(folded)):
                raise ValueError(f"ecs_clusters: duplicate {kind} name across clusters.")
        discovery = {key for cluster in clusters for key in cluster.discovery_keys}
        for cluster in clusters:
            for task in cluster.task_definitions:
                if task.cloud_map_service is not None and task.cloud_map_service not in discovery:
                    raise ValueError(
                        f"ECS task definition '{task.family}': unknown cloud_map_service "
                        f"'{task.cloud_map_service}'. Configured: {', '.join(sorted(discovery)) or 'none'}."
                    )

    def __bool__(self) -> bool:
        return any(cluster.create for cluster in self.clusters)

    @property
    def enabled_clusters(self) -> tuple[EcsClusterProps, ...]:
        return tuple(cluster for cluster in self.clusters if cluster.create)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EcsProps:
        return cls(clusters=schema.items(
            raw, "ecs_clusters", lambda item: EcsClusterProps.from_dict(item, base_dir=base_dir)
        ))


# --- synthesis --------------------------------------------------------------


def to_cloudformation(value: Any, *, freeform: bool = False) -> Any:
    """ECS-API camelCase container JSON -> CloudFormation PascalCase, keeping free-form maps."""
    if isinstance(value, Mapping):
        if freeform:
            return dict(value)
        return {
            key[:1].upper() + key[1:]: to_cloudformation(item, freeform=key in FREEFORM_KEYS)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [to_cloudformation(item) for item in value]
    return value


def _substitute(value: Any, tokens: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for token, replacement in tokens.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, Mapping):
        return {key: _substitute(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, tokens) for item in value]
    return value


def _tags(*mappings: Mapping[str, str]) -> list[cdk.CfnTag]:
    merged: dict[str, str] = {}
    for mapping in mappings:
        merged.update(mapping)
    return [cdk.CfnTag(key=key, value=value) for key, value in merged.items()]


class EcsClusters(Construct):
    """Clusters, task definitions, services, Cloud Map and EC2 capacity."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        props: EcsProps,
        common: Common,
        name: Callable[..., str],
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        roles: Mapping[str, iam.IRole] | None = None,
        target_groups: Mapping[str, elbv2.ITargetGroup] | None = None,
        repositories: Mapping[str, ecr.IRepository] | None = None,
        key_pairs: Mapping[str, ec2.IKeyPair] | None = None,
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
        load_balancers: Mapping[str, elbv2.BaseLoadBalancer] | None = None,
        instance_profiles: Mapping[str, iam.CfnInstanceProfile] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        self._common = common
        self._name = name
        self._network = network
        self._security_groups = security_groups
        self._roles = roles or {}
        self._target_groups = target_groups or {}
        self._repositories = repositories or {}
        self._key_pairs = key_pairs or {}
        self._secrets = secrets or {}
        self._load_balancers = load_balancers or {}
        self._instance_profiles = instance_profiles or {}

        self.clusters: dict[str, ecs.ICluster] = {}
        self.cluster_resources: dict[str, ecs.CfnCluster] = {}
        self.task_definitions: dict[str, ecs.CfnTaskDefinition] = {}
        self.services: dict[str, ecs.CfnService] = {}
        self.log_groups: dict[str, logs.CfnLogGroup] = {}
        self.namespaces: dict[str, cdk.CfnResource] = {}
        self.discovery_services: dict[str, servicediscovery.CfnService] = {}
        self.launch_templates: dict[str, ec2.CfnLaunchTemplate] = {}
        self.auto_scaling_groups: dict[str, autoscaling.CfnAutoScalingGroup] = {}
        self.capacity_providers: dict[str, ecs.CfnCapacityProvider] = {}
        self.capacity_provider_associations: dict[str, ecs.CfnClusterCapacityProviderAssociations] = {}
        self.scaling_policies: dict[str, autoscaling.CfnScalingPolicy] = {}

        for cluster in props.enabled_clusters:
            self._cluster(cluster)

    # --- helpers -----------------------------------------------------------

    def _resource_name(self, label: str, *parts: str) -> str:
        full = self._name(*parts)
        if not RESOURCE_NAME.fullmatch(full):
            raise ValueError(
                f"{label}: final name '{full}' must be 1-255 letters, digits, '-' or '_'."
            )
        return full

    def _subnet_ids(self, tier: str, label: str) -> list[str]:
        subnets = (
            self._network.private_subnets if tier == "private" else self._network.public_subnets
        )
        if not subnets:
            raise ValueError(f"{label}: the network has no {tier} subnets.")
        return [subnet.subnet_id for subnet in subnets]

    def _group_ids(self, keys: tuple[str, ...], label: str) -> list[str]:
        return [
            schema.lookup(self._security_groups, key, f"{label}: security group").security_group_id
            for key in keys
        ]

    def _secret_arn(self, key: str, label: str) -> str:
        return schema.lookup(self._secrets, key, f"{label}: secret").secret_arn

    # --- cluster -----------------------------------------------------------

    def _cluster(self, cluster: EcsClusterProps) -> None:
        label = f"ECS cluster '{cluster.name}'"
        base = logical_id(cluster.name)
        full_name = self._resource_name(label, cluster.name)
        configuration = None
        if cluster.execute_command is not None:
            command = cluster.execute_command
            configuration = ecs.CfnCluster.ClusterConfigurationProperty(
                execute_command_configuration=ecs.CfnCluster.ExecuteCommandConfigurationProperty(
                    kms_key_id=command.kms_key_arn,
                    logging=command.logging,
                    log_configuration=(
                        ecs.CfnCluster.ExecuteCommandLogConfigurationProperty(
                            cloud_watch_encryption_enabled=command.cloud_watch_encryption_enabled,
                            cloud_watch_log_group_name=command.cloud_watch_log_group_name,
                            s3_bucket_name=command.s3_bucket_name,
                            s3_encryption_enabled=command.s3_encryption_enabled,
                            s3_key_prefix=command.s3_key_prefix,
                        )
                        if command.has_log_configuration else None
                    ),
                )
            )
        resource = ecs.CfnCluster(
            self,
            f"{base}Cluster",
            cluster_name=full_name,
            cluster_settings=(
                [ecs.CfnCluster.ClusterSettingsProperty(name="containerInsights", value="enabled")]
                if cluster.container_insights else None
            ),
            configuration=configuration,
        )
        cdk.Tags.of(resource).add("Name", full_name)
        self.cluster_resources[cluster.name] = resource
        self.clusters[cluster.name] = ecs.Cluster.from_cluster_attributes(
            self,
            f"{base}ClusterReference",
            cluster_name=resource.ref,
            cluster_arn=resource.attr_arn,
            vpc=self._network.vpc,
        )

        for namespace in cluster.namespaces:
            self._namespace(namespace)

        provider = self._capacity(cluster, full_name) if cluster.ec2_autoscaling else None

        def provider_name(value: str) -> str:
            return value if value in FARGATE_PROVIDERS or provider is None else provider.ref

        association = None
        if cluster.has_capacity_providers:
            association = ecs.CfnClusterCapacityProviderAssociations(
                self,
                f"{base}CapacityProviderAssociations",
                cluster=resource.ref,
                capacity_providers=[provider_name(item) for item in cluster.capacity_provider_names],
                default_capacity_provider_strategy=[
                    ecs.CfnClusterCapacityProviderAssociations.CapacityProviderStrategyProperty(
                        capacity_provider=provider_name(item.capacity_provider),
                        base=item.base,
                        weight=item.weight,
                    )
                    for item in cluster.default_strategy
                ],
            )
            self.capacity_provider_associations[cluster.name] = association

        for task in cluster.task_definitions:
            self._task_definition(task, full_name)
        for service in cluster.services:
            self._service(service, cluster, resource, association, provider_name)

    def _namespace(self, namespace: NamespaceProps) -> None:
        base = logical_id(namespace.name)
        resource: Any
        if namespace.type == "DNS_PRIVATE":
            resource = servicediscovery.CfnPrivateDnsNamespace(
                self, f"{base}Namespace", name=namespace.name,
                vpc=self._network.vpc.vpc_id, description=namespace.description,
            )
        elif namespace.type == "DNS_PUBLIC":
            resource = servicediscovery.CfnPublicDnsNamespace(
                self, f"{base}Namespace", name=namespace.name, description=namespace.description,
            )
        else:
            resource = servicediscovery.CfnHttpNamespace(
                self, f"{base}Namespace", name=namespace.name, description=namespace.description,
            )
        cdk.Tags.of(resource).add("Name", namespace.name)
        self.namespaces[namespace.name] = resource
        for service in namespace.services:
            check = service.health_check
            discovery = servicediscovery.CfnService(
                self,
                f"{base}{logical_id(service.name)}DiscoveryService",
                name=service.name,
                namespace_id=resource.attr_id,
                dns_config=(
                    servicediscovery.CfnService.DnsConfigProperty(
                        namespace_id=resource.attr_id,
                        routing_policy=service.routing_policy,
                        dns_records=[
                            servicediscovery.CfnService.DnsRecordProperty(type=kind, ttl=ttl)
                            for kind, ttl in service.dns_records
                        ],
                    )
                    if service.has_dns_config else None
                ),
                health_check_config=(
                    servicediscovery.CfnService.HealthCheckConfigProperty(
                        type=check[0], resource_path=check[1], failure_threshold=check[2]
                    )
                    if check is not None else None
                ),
                health_check_custom_config=(
                    servicediscovery.CfnService.HealthCheckCustomConfigProperty(
                        failure_threshold=service.custom_health_check_threshold
                    )
                    if service.custom_health_check_threshold is not None else None
                ),
            )
            cdk.Tags.of(discovery).add("Name", service.name)
            self.discovery_services[f"{namespace.name}/{service.name}"] = discovery

    # --- EC2 capacity --------------------------------------------------------

    def _capacity(self, cluster: EcsClusterProps, cluster_name: str) -> ecs.CfnCapacityProvider:
        settings = cluster.ec2_autoscaling
        assert settings is not None
        label = f"ECS cluster '{cluster.name}'"
        base = logical_id(cluster.name)
        for template in settings.launch_templates:
            self._launch_template(template, cluster_name)
        group = settings.autoscaling_group
        provider = settings.capacity_provider
        group_name = self._resource_name(f"{label}, autoscaling group", group.name, "asg")
        template = self.launch_templates[settings.launch_template.name]
        asg = autoscaling.CfnAutoScalingGroup(
            self,
            f"{base}AutoScalingGroup",
            auto_scaling_group_name=group_name,
            min_size=str(group.min_size),
            max_size=str(group.max_size),
            desired_capacity=str(group.desired_capacity) if group.desired_capacity is not None else None,
            health_check_type=group.health_check_type,
            health_check_grace_period=group.health_check_grace_period,
            vpc_zone_identifier=self._subnet_ids(group.subnets, f"{label}, autoscaling group"),
            target_group_arns=[
                schema.lookup(self._target_groups, key, f"{label}, autoscaling group: target group").target_group_arn
                for key in group.target_groups
            ] or None,
            launch_template=autoscaling.CfnAutoScalingGroup.LaunchTemplateSpecificationProperty(
                launch_template_id=template.ref,
                version=template.attr_latest_version_number,
            ),
            new_instances_protected_from_scale_in=(
                True if provider.managed_termination_protection == "ENABLED" else None
            ),
            tags=[
                autoscaling.CfnAutoScalingGroup.TagPropertyProperty(
                    key=key, value=value, propagate_at_launch=propagate
                )
                for key, value, propagate in (
                    *((key, value, True) for key, value in group.tags.items()),
                    *group.additional_tags,
                )
            ] or None,
        )
        cdk.Tags.of(asg).add("Name", group_name, apply_to_launched_instances=False)
        self.auto_scaling_groups[cluster.name] = asg

        provider_name = self._resource_name(f"{label}, capacity provider", provider.name)
        if re.match(r"(?i)(aws|ecs|fargate)", provider_name):
            raise ValueError(f"{label}: capacity provider name '{provider_name}' must not start with aws, ecs or fargate.")
        capacity = ecs.CfnCapacityProvider(
            self,
            f"{base}CapacityProvider",
            name=provider_name,
            auto_scaling_group_provider=ecs.CfnCapacityProvider.AutoScalingGroupProviderProperty(
                auto_scaling_group_arn=asg.ref,
                managed_termination_protection=provider.managed_termination_protection,
                managed_scaling=ecs.CfnCapacityProvider.ManagedScalingProperty(
                    maximum_scaling_step_size=provider.maximum_scaling_step_size,
                    minimum_scaling_step_size=provider.minimum_scaling_step_size,
                    status=provider.status,
                    target_capacity=provider.target_capacity,
                    instance_warmup_period=provider.instance_warmup_period,
                ),
            ),
        )
        cdk.Tags.of(capacity).add("Name", provider_name)
        self.capacity_providers[cluster.name] = capacity

        for direction, policy in (("up", settings.scale_up), ("down", settings.scale_down)):
            if policy is None:
                continue
            self.scaling_policies[f"{cluster.name}-scale-{direction}"] = autoscaling.CfnScalingPolicy(
                self,
                f"{base}Scale{direction.capitalize()}Policy",
                auto_scaling_group_name=asg.ref,
                policy_type="SimpleScaling",
                adjustment_type=policy.adjustment_type,
                scaling_adjustment=policy.scaling_adjustment,
                cooldown=str(policy.cooldown),
            )
        return capacity

    def _launch_template(self, template: LaunchTemplateProps, cluster_name: str) -> None:
        label = f"ECS launch template '{template.name}'"
        full_name = self._resource_name(label, template.name, "lt")
        image_id = template.ami_id or ec2.MachineImage.from_ssm_parameter(
            template.ami_ssm_parameter or AMIS[template.ami or "ECSAL2023"]
        ).get_image(self).image_id
        groups = self._group_ids(template.security_groups, label)
        user_data = (
            render(template.user_data, {"cluster_name": cluster_name}, f"{label}: user_data")
            if template.user_data is not None
            else f"#!/bin/bash\necho ECS_CLUSTER={cluster_name} >> /etc/ecs/ecs.config\n"
        )
        common_tags = dict(self._common.tags)
        instance_tags = _tags(common_tags, {"Name": full_name}, template.tags)
        data = ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
            image_id=image_id,
            instance_type=template.instance_type,
            key_name=(
                schema.lookup(self._key_pairs, template.key_pair, f"{label}: key_pair").key_pair_name
                if template.key_pair else None
            ),
            iam_instance_profile=(
                ec2.CfnLaunchTemplate.IamInstanceProfileProperty(
                    arn=schema.lookup(
                        self._instance_profiles, template.instance_profile, f"{label}: instance_profile"
                    ).attr_arn
                )
                if template.instance_profile else None
            ),
            security_group_ids=(groups or None) if template.associate_public_ip_address is None else None,
            network_interfaces=(
                [
                    ec2.CfnLaunchTemplate.NetworkInterfaceProperty(
                        device_index=0,
                        associate_public_ip_address=template.associate_public_ip_address,
                        groups=groups or None,
                        delete_on_termination=True,
                    )
                ]
                if template.associate_public_ip_address is not None else None
            ),
            user_data=cdk.Fn.base64(user_data),
            block_device_mappings=(
                [
                    ec2.CfnLaunchTemplate.BlockDeviceMappingProperty(
                        device_name=template.root_device_name,
                        ebs=ec2.CfnLaunchTemplate.EbsProperty(
                            volume_size=template.volume_size,
                            volume_type="gp3",
                            delete_on_termination=True,
                            encrypted=True,
                        ),
                    )
                ]
                if template.volume_size is not None else None
            ),
            metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                http_endpoint="enabled",
                http_tokens="required" if template.require_imdsv2 else "optional",
                http_put_response_hop_limit=2,
            ),
            tag_specifications=[
                ec2.CfnLaunchTemplate.TagSpecificationProperty(resource_type=kind, tags=instance_tags)
                for kind in ("instance", "volume")
            ],
        )
        self.launch_templates[template.name] = ec2.CfnLaunchTemplate(
            self,
            f"{logical_id(template.name)}LaunchTemplate",
            launch_template_name=full_name,
            launch_template_data=data,
            tag_specifications=[
                ec2.CfnLaunchTemplate.LaunchTemplateTagSpecificationProperty(
                    resource_type="launch-template",
                    tags=_tags(common_tags, {"Name": full_name}),
                )
            ],
        )

    # --- task definitions and services ---------------------------------------

    def _role_arn(self, key: str | None, arn: str | None, label: str) -> str | None:
        if key is not None:
            return schema.lookup(self._roles, key, label).role_arn
        return arn

    def _backend_url(self, task: TaskDefinitionProps, label: str) -> str | None:
        if task.backend_load_balancer is not None:
            balancer = schema.lookup(
                self._load_balancers, task.backend_load_balancer, f"{label}: backend_load_balancer"
            )
            port = f":{task.backend_load_balancer_port}" if task.backend_load_balancer_port else ""
            return f"http://{balancer.load_balancer_dns_name}{port}"
        if task.cloud_map_service is not None:
            namespace, service = task.cloud_map_service.split("/")
            port = f":{task.cloud_map_port}" if task.cloud_map_port else ""
            return f"http://{service}.{namespace}{port}"
        return task.backend_url

    def _frontend_url(self, task: TaskDefinitionProps, label: str) -> str | None:
        if task.frontend_load_balancer is not None:
            balancer = schema.lookup(
                self._load_balancers, task.frontend_load_balancer, f"{label}: frontend_load_balancer"
            )
            return f"http://{balancer.load_balancer_dns_name}"
        return task.frontend_url

    def _log_group(self, task: TaskDefinitionProps, family: str) -> str:
        if task.family not in self.log_groups:
            group = logs.CfnLogGroup(
                self,
                f"{logical_id(task.family)}LogGroup",
                log_group_name=f"/ecs/{family}",
                retention_in_days=task.log_retention_days,
            )
            group.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
            self.log_groups[task.family] = group
        return self.log_groups[task.family].ref

    def _inline_container(
        self, container: ContainerProps, task: TaskDefinitionProps, family: str, label: str,
        backend_url: str | None,
    ) -> dict[str, Any]:
        where = f"{label}, container '{container.name}'"
        definition: dict[str, Any] = {"name": container.name, **container.definition}
        if container.repository is not None:
            repository = schema.lookup(self._repositories, container.repository, f"{where}: repository")
            definition["image"] = f"{repository.repository_uri}:{container.image_tag}"
        else:
            definition["image"] = container.image
        environment = [{"name": key, "value": value} for key, value in container.environment]
        if backend_url is not None:
            environment.append({"name": "BACKEND_URL", "value": backend_url})
        if environment:
            definition["environment"] = environment
        secrets = []
        for secret in container.secrets:
            if secret.secret is not None:
                arn = self._secret_arn(secret.secret, f"{where}: secrets")
                value_from = f"{arn}:{secret.key}::" if secret.key else arn
            else:
                value_from = secret.value_from
            secrets.append({"name": secret.name, "valueFrom": value_from})
        if task.database_secret is not None:
            arn = self._secret_arn(task.database_secret, f"{label}: database_secret")
            secrets += [{"name": name, "valueFrom": f"{arn}:{field}::"} for name, field in DB_FIELDS]
        if secrets:
            definition["secrets"] = secrets
        if container.log_driver is not None:
            options = dict(container.log_options)
            if container.log_driver == "awslogs":
                options.setdefault("awslogs-group", self._log_group(task, family))
                options.setdefault("awslogs-region", cdk.Stack.of(self).region)
                options.setdefault("awslogs-stream-prefix", container.name)
            definition["logConfiguration"] = {"logDriver": container.log_driver, "options": options}
        return definition

    def _file_containers(
        self, task: TaskDefinitionProps, cluster_name: str, label: str, backend_url: str | None,
    ) -> list[Any]:
        stack = cdk.Stack.of(self)
        values: dict[str, Any] = {
            "account_number": stack.account,
            "region": stack.region,
            "account_name_abr": self._common.account_name_abr,
            "cluster_name": cluster_name,
        }
        for name in placeholders(list(task.container_file)):
            match = REFERENCE_PLACEHOLDER.fullmatch(name)
            if match is None:
                continue
            kind, key = match.groups()
            if kind == "repository":
                values[name] = schema.lookup(self._repositories, key, f"{label}: [[{name}]]").repository_uri
            else:
                values[name] = self._secret_arn(key, f"{label}: [[{name}]]")
        rendered = render(list(task.container_file), values, label)
        tokens = {
            "__BACKEND_URL__": backend_url,
            "__SECRET_ARN__": self._secret_arn(task.database_secret, f"{label}: database_secret")
            if task.database_secret else None,
            "__FRONTEND_URL__": self._frontend_url(task, label),
            "__SMTP_SECRET_ARN__": self._secret_arn(task.smtp_secret, f"{label}: smtp_secret")
            if task.smtp_secret else None,
        }
        return _substitute(rendered, {token: value for token, value in tokens.items() if value})

    @staticmethod
    def _volume(volume: VolumeProps) -> dict[str, Any]:
        result: dict[str, Any] = {"Name": volume.name}
        if volume.host_path is not None:
            result["Host"] = {"SourcePath": volume.host_path}
        if volume.docker is not None:
            docker = {
                "Scope": volume.docker["scope"],
                "Autoprovision": volume.docker["autoprovision"],
                "Driver": volume.docker["driver"],
                "DriverOpts": volume.docker["driver_opts"],
                "Labels": volume.docker["labels"],
            }
            result["DockerVolumeConfiguration"] = {k: v for k, v in docker.items() if v is not None}
        if volume.efs is not None:
            efs = volume.efs
            config: dict[str, Any] = {
                "FilesystemId": efs.file_system_id,
                "RootDirectory": efs.root_directory,
                "TransitEncryption": efs.transit_encryption,
                "TransitEncryptionPort": efs.transit_encryption_port,
            }
            if efs.access_point_id or efs.iam:
                config["AuthorizationConfig"] = {
                    k: v for k, v in {"AccessPointId": efs.access_point_id, "IAM": efs.iam}.items()
                    if v is not None
                }
            result["EFSVolumeConfiguration"] = {k: v for k, v in config.items() if v is not None}
        return result

    def _task_definition(self, task: TaskDefinitionProps, cluster_name: str) -> None:
        label = f"ECS task definition '{task.family}'"
        family = self._resource_name(label, task.family)
        backend_url = self._backend_url(task, label)
        if task.containers:
            containers = [
                self._inline_container(container, task, family, label, backend_url)
                for container in task.containers
            ]
        else:
            containers = self._file_containers(task, cluster_name, label, backend_url)

        resource = ecs.CfnTaskDefinition(
            self,
            f"{logical_id(task.family)}TaskDefinition",
            family=family,
            task_role_arn=self._role_arn(task.task_role, task.task_role_arn, f"{label}: task_role"),
            execution_role_arn=self._role_arn(
                task.execution_role, task.execution_role_arn, f"{label}: execution_role"
            ),
            network_mode=task.network_mode,
            requires_compatibilities=list(task.requires_compatibilities),
            cpu=task.cpu,
            memory=task.memory,
            runtime_platform=(
                ecs.CfnTaskDefinition.RuntimePlatformProperty(
                    operating_system_family=task.operating_system_family,
                    cpu_architecture=task.cpu_architecture,
                )
                if task.operating_system_family or task.cpu_architecture else None
            ),
            placement_constraints=[
                ecs.CfnTaskDefinition.TaskDefinitionPlacementConstraintProperty(
                    type=kind, expression=expression
                )
                for kind, expression in task.placement_constraints
            ] or None,
            proxy_configuration=(
                ecs.CfnTaskDefinition.ProxyConfigurationProperty(
                    container_name=task.proxy_container,
                    type=task.proxy_type,
                    proxy_configuration_properties=[
                        ecs.CfnTaskDefinition.KeyValuePairProperty(name=key, value=value)
                        for key, value in task.proxy_properties.items()
                    ] or None,
                )
                if task.proxy_container else None
            ),
        )
        # Container JSON stays in ECS's own shape, so it is passed through as-is.
        resource.add_property_override("ContainerDefinitions", to_cloudformation(containers))
        if task.volumes:
            resource.add_property_override("Volumes", [self._volume(volume) for volume in task.volumes])
        cdk.Tags.of(resource).add("Name", family)
        self.task_definitions[task.family] = resource

    def _service(
        self,
        service: ServiceProps,
        cluster: EcsClusterProps,
        cluster_resource: ecs.CfnCluster,
        association: ecs.CfnClusterCapacityProviderAssociations | None,
        provider_name: Callable[[str], str],
    ) -> None:
        label = f"ECS service '{service.name}'"
        name = self._resource_name(label, service.name)
        task_definition = (
            self.task_definitions[service.task_definition].ref
            if service.task_definition is not None else service.task_definition_arn
        )
        breaker = (
            ecs.CfnService.DeploymentCircuitBreakerProperty(
                enable=service.circuit_breaker[0], rollback=service.circuit_breaker[1]
            )
            if service.circuit_breaker is not None else None
        )
        registries = None
        if service.registry is not None:
            registry = service.registry
            registry_arn = (
                self.discovery_services[registry.cloud_map_service].attr_arn
                if registry.cloud_map_service else registry.registry_arn
            )
            registries = [ecs.CfnService.ServiceRegistryProperty(
                registry_arn=registry_arn,
                port=registry.port,
                container_name=registry.container_name,
                container_port=registry.container_port,
            )]
        resource = ecs.CfnService(
            self,
            f"{logical_id(service.name)}Service",
            service_name=name,
            cluster=cluster_resource.ref,
            task_definition=task_definition,
            desired_count=service.desired_count,
            launch_type=service.launch_type,
            platform_version=service.platform_version,
            scheduling_strategy=service.scheduling_strategy,
            enable_ecs_managed_tags=service.enable_ecs_managed_tags,
            enable_execute_command=service.enable_execute_command,
            health_check_grace_period_seconds=service.health_check_grace_period_seconds,
            propagate_tags=service.propagate_tags,
            capacity_provider_strategy=[
                ecs.CfnService.CapacityProviderStrategyItemProperty(
                    capacity_provider=provider_name(item.capacity_provider),
                    weight=item.weight,
                    base=item.base,
                )
                for item in service.capacity_provider_strategy
            ] or None,
            deployment_configuration=ecs.CfnService.DeploymentConfigurationProperty(
                maximum_percent=service.maximum_percent,
                minimum_healthy_percent=service.minimum_healthy_percent,
                deployment_circuit_breaker=breaker,
            ),
            deployment_controller=(
                ecs.CfnService.DeploymentControllerProperty(type=service.deployment_controller)
                if service.deployment_controller else None
            ),
            load_balancers=[
                ecs.CfnService.LoadBalancerProperty(
                    target_group_arn=schema.lookup(
                        self._target_groups, target_group, f"{label}: target_group"
                    ).target_group_arn,
                    container_name=container,
                    container_port=port,
                )
                for target_group, container, port in service.load_balancers
            ] or None,
            network_configuration=(
                ecs.CfnService.NetworkConfigurationProperty(
                    awsvpc_configuration=ecs.CfnService.AwsVpcConfigurationProperty(
                        subnets=self._subnet_ids(service.subnets or "private", label),
                        security_groups=self._group_ids(service.security_groups, label),
                        assign_public_ip="ENABLED" if service.assign_public_ip else "DISABLED",
                    )
                )
                if service.security_groups else None
            ),
            placement_constraints=[
                ecs.CfnService.PlacementConstraintProperty(type=kind, expression=expression)
                for kind, expression in service.placement_constraints
            ] or None,
            placement_strategies=[
                ecs.CfnService.PlacementStrategyProperty(type=kind, field=value)
                for kind, value in service.placement_strategies
            ] or None,
            service_registries=registries,
        )
        if association is not None:
            resource.add_resource_dependency(association)
        cdk.Tags.of(resource).add("Name", name)
        self.services[service.name] = resource
