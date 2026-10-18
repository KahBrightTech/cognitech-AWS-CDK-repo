"""Config-driven RDS database instances, mirroring the Terraform `RDS` module.

```yaml
rds_instances:
  - name: app-postgres
    engine: postgres
    engine_version: "15.16"
    instance_class: db.t3.micro
    allocated_storage: 20
    max_allocated_storage: 40
    database_name: ecsdb
    port: 5432
    subnet_type: private          # public | private tier of the Network
    security_groups: [db]         # names from security_groups, or sg- ids
```

The master password is never written to the template. By default CDK asks
Secrets Manager to generate it (`password_management: generated`) and attaches
the secret to the instance, so the secret holds `username`, `password`,
`host`, `port`, `dbname` and `engine` like the Terraform module's secret.
`password_management: rds_managed` lets RDS own the secret instead, and
`master_user_secret` reuses a secret from the secrets stack.

Resource names:

    {account_name}-{region_prefix}-{vpc_name}-{name}
    {account_name}-{region_prefix}-{vpc_name}-{name}-replica
    {account_name}-{region_prefix}-{vpc_name}-{name}-subnet-group
    {account_name}-{region_prefix}-{vpc_name}-{name}-parameter-group
    {account_name}-{region_prefix}-{vpc_name}-{name}/rds-credentials
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.config import ORDINALS, Common
from cognitech_cdk.constructs.network import Network

ENGINES = {
    "mysql": (rds.DatabaseInstanceEngine.mysql, rds.MysqlEngineVersion, 2),
    "mariadb": (rds.DatabaseInstanceEngine.maria_db, rds.MariaDbEngineVersion, 2),
    "postgres": (rds.DatabaseInstanceEngine.postgres, rds.PostgresEngineVersion, 1),
    "oracle-ee": (rds.DatabaseInstanceEngine.oracle_ee, rds.OracleEngineVersion, 1),
    "oracle-se2": (rds.DatabaseInstanceEngine.oracle_se2, rds.OracleEngineVersion, 1),
    "sqlserver-ee": (rds.DatabaseInstanceEngine.sql_server_ee, rds.SqlServerEngineVersion, 2),
    "sqlserver-se": (rds.DatabaseInstanceEngine.sql_server_se, rds.SqlServerEngineVersion, 2),
    "sqlserver-ex": (rds.DatabaseInstanceEngine.sql_server_ex, rds.SqlServerEngineVersion, 2),
    "sqlserver-web": (rds.DatabaseInstanceEngine.sql_server_web, rds.SqlServerEngineVersion, 2),
}
STORAGE_TYPES = ("gp2", "gp3", "io1", "io2", "standard")
PASSWORD_MANAGEMENT = ("generated", "rds_managed")
REMOVAL_POLICIES = {
    "retain": cdk.RemovalPolicy.RETAIN,
    "snapshot": cdk.RemovalPolicy.SNAPSHOT,
    "destroy": cdk.RemovalPolicy.DESTROY,
}
SUBNET_TYPES = ("public", "private")
MONITORING_INTERVALS = (0, 1, 5, 10, 15, 30, 60)
PERFORMANCE_INSIGHTS_RETENTION = (7, 731, *(31 * months for months in range(1, 24)))
IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
DATABASE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,15}")
WINDOW = re.compile(r"(?:(?:mon|tue|wed|thu|fri|sat|sun):)?\d{2}:\d{2}-(?:(?:mon|tue|wed|thu|fri|sat|sun):)?\d{2}:\d{2}")
SECURITY_GROUP_ID = re.compile(r"sg-[0-9a-f]+")
KMS_ARN = re.compile(r"arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:(?:key|alias)/[A-Za-z0-9/_-]+")
ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+")

FIELDS = {
    "name", "create", "engine", "engine_version", "engine_major_version", "instance_class",
    "allocated_storage", "max_allocated_storage", "storage_type", "storage_encrypted",
    "kms_key_arn", "iops", "database_name", "master_username", "password_management",
    "master_user_secret", "secrets_kms_key_arn", "port", "subnet_type", "subnet_positions",
    "security_groups", "publicly_accessible", "multi_az", "availability_zone",
    "parameter_group_name", "parameter_group_family", "parameters", "option_group_name",
    "backup_retention_period", "backup_window", "maintenance_window",
    "auto_minor_version_upgrade", "deletion_protection", "removal_policy",
    "copy_tags_to_snapshot", "enabled_cloudwatch_logs_exports", "monitoring_interval",
    "monitoring_role_arn", "performance_insights_enabled", "performance_insights_kms_key_arn",
    "performance_insights_retention_period", "apply_immediately", "create_read_replica",
    "replica_instance_class", "tags",
}


def construct_id(name: str) -> str:
    """`app-postgres` -> `AppPostgres`, so logical ids stay readable."""
    return "".join(part.capitalize() for part in re.split(r"[-_\s]+", name) if part)


def _choice(value: Any, label: str, choices: tuple[Any, ...], default: Any) -> Any:
    if value is None:
        return default
    if value not in choices:
        raise ValueError(f"{label} must be one of {', '.join(map(str, choices))}, got '{value}'.")
    return value


def _pattern(value: Any, label: str, pattern: re.Pattern[str], hint: str) -> str | None:
    result = schema.string(value, label)
    if result is not None and not pattern.fullmatch(result):
        raise ValueError(f"{label} must be {hint}, got '{result}'.")
    return result


def resolve_subnets(
    network: Network, subnet_type: str, positions: tuple[str, ...], label: str
) -> list[ec2.ISubnet]:
    """Subnets of one tier, optionally limited to AZ positions (`primary`, ...)."""
    subnets = list(network.public_subnets if subnet_type == "public" else network.private_subnets)
    if not subnets:
        raise ValueError(f"{label}: the network has no {subnet_type} subnets.")
    if not positions:
        return subnets
    chosen = []
    for position in positions:
        index = ORDINALS.index(position)
        if index >= len(subnets):
            raise ValueError(
                f"{label}: subnet position '{position}' is not available; the network "
                f"has {len(subnets)} {subnet_type} subnet(s)."
            )
        chosen.append(subnets[index])
    return chosen


def resolve_security_groups(
    scope: Construct,
    groups: Mapping[str, ec2.ISecurityGroup],
    names: tuple[str, ...],
    label: str,
) -> list[ec2.ISecurityGroup]:
    resolved = []
    for name in names:
        if SECURITY_GROUP_ID.fullmatch(name):
            resolved.append(
                ec2.SecurityGroup.from_security_group_id(
                    scope, f"{construct_id(label)}{name}", name, mutable=False
                )
            )
        else:
            resolved.append(schema.lookup(groups, name, f"{label} security_groups"))
    return resolved


@dataclass(frozen=True)
class RdsInstanceProps:
    name: str
    engine: str
    engine_version: str
    instance_class: str
    allocated_storage: int
    security_groups: tuple[str, ...]
    engine_major_version: str | None = None
    create: bool = True
    max_allocated_storage: int | None = None
    storage_type: str = "gp3"
    storage_encrypted: bool = True
    kms_key_arn: str | None = None
    iops: int | None = None
    database_name: str | None = None
    master_username: str = "dbadmin"
    password_management: str = "generated"
    master_user_secret: str | None = None
    secrets_kms_key_arn: str | None = None
    port: int | None = None
    subnet_type: str = "private"
    subnet_positions: tuple[str, ...] = ()
    publicly_accessible: bool = False
    multi_az: bool = False
    availability_zone: str | None = None
    parameter_group_name: str | None = None
    parameter_group_family: str | None = None
    parameters: Mapping[str, str] = field(default_factory=dict)
    option_group_name: str | None = None
    backup_retention_period: int = 7
    backup_window: str | None = None
    maintenance_window: str | None = None
    auto_minor_version_upgrade: bool = True
    deletion_protection: bool = True
    removal_policy: str = "retain"
    copy_tags_to_snapshot: bool = True
    enabled_cloudwatch_logs_exports: tuple[str, ...] = ()
    monitoring_interval: int = 0
    monitoring_role_arn: str | None = None
    performance_insights_enabled: bool = False
    performance_insights_kms_key_arn: str | None = None
    performance_insights_retention_period: int | None = None
    apply_immediately: bool = False
    create_read_replica: bool = False
    replica_instance_class: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def major_version(self) -> str:
        if self.engine_major_version:
            return self.engine_major_version
        parts = ENGINES[self.engine][2]
        return ".".join(self.engine_version.split(".")[:parts])

    @classmethod
    def from_dict(cls, raw: Any) -> RdsInstanceProps:
        data = schema.mapping(
            raw, "RDS instance", FIELDS,
            required=("name", "engine", "engine_version", "instance_class", "allocated_storage"),
        )
        name = schema.name(data["name"], "RDS instance name")
        label = f"RDS instance '{name}'"
        engine = _choice(data["engine"], f"{label}: engine", tuple(ENGINES), None)
        engine_version = schema.required_string(data["engine_version"], f"{label}: engine_version")
        if not re.fullmatch(r"\d+(?:\.[0-9A-Za-z_-]+)*", engine_version):
            raise ValueError(f"{label}: engine_version must look like 8.0.43 or 15.16.")
        instance_class = _pattern(
            data["instance_class"], f"{label}: instance_class",
            re.compile(r"db\.[a-z0-9-]+\.[a-z0-9]+"), "an RDS class such as db.t3.micro",
        )
        replica_class = _pattern(
            data.get("replica_instance_class"), f"{label}: replica_instance_class",
            re.compile(r"db\.[a-z0-9-]+\.[a-z0-9]+"), "an RDS class such as db.t3.micro",
        )
        allocated = schema.integer(
            data["allocated_storage"], f"{label}: allocated_storage", minimum=5, maximum=65536
        )
        maximum = schema.integer(
            data.get("max_allocated_storage"), f"{label}: max_allocated_storage", maximum=65536
        )
        if maximum is not None and maximum < allocated:
            raise ValueError(f"{label}: max_allocated_storage must be at least allocated_storage.")
        storage_type = _choice(data.get("storage_type"), f"{label}: storage_type", STORAGE_TYPES, "gp3")
        iops = schema.integer(data.get("iops"), f"{label}: iops", minimum=1000)
        if storage_type in ("io1", "io2") and iops is None:
            raise ValueError(f"{label}: storage_type {storage_type} requires iops.")
        if storage_type in ("gp2", "standard") and iops is not None:
            raise ValueError(f"{label}: iops is only valid for gp3, io1 and io2 storage.")
        storage_encrypted = schema.boolean(
            data.get("storage_encrypted"), f"{label}: storage_encrypted", default=True
        )
        kms_hint = "a KMS key or alias ARN"
        kms_key_arn = _pattern(data.get("kms_key_arn"), f"{label}: kms_key_arn", KMS_ARN, kms_hint)
        if kms_key_arn and not storage_encrypted:
            raise ValueError(f"{label}: kms_key_arn requires storage_encrypted: true.")

        database_name = _pattern(
            data.get("database_name"), f"{label}: database_name", DATABASE_NAME,
            "a letter followed by up to 63 letters, digits or '_'",
        )
        if database_name and engine.startswith("sqlserver"):
            raise ValueError(f"{label}: SQL Server engines do not accept database_name.")
        username = _pattern(
            data.get("master_username"), f"{label}: master_username", USERNAME,
            "a letter followed by up to 15 letters, digits or '_'",
        ) or "dbadmin"
        if username.lower() in ("rdsadmin", "rdsrepladmin", "rds_superuser"):
            raise ValueError(f"{label}: master_username '{username}' is reserved by RDS.")
        password_management = _choice(
            data.get("password_management"), f"{label}: password_management",
            PASSWORD_MANAGEMENT, "generated",
        )
        master_user_secret = data.get("master_user_secret")
        if master_user_secret is not None:
            master_user_secret = schema.name(master_user_secret, f"{label}: master_user_secret")
            if "password_management" in data or "master_username" in data:
                raise ValueError(
                    f"{label}: master_user_secret supplies the username and password; "
                    "leave out password_management and master_username."
                )
        secrets_kms = _pattern(
            data.get("secrets_kms_key_arn"), f"{label}: secrets_kms_key_arn", KMS_ARN, kms_hint
        )
        if secrets_kms and master_user_secret:
            raise ValueError(f"{label}: secrets_kms_key_arn does not apply to master_user_secret.")

        subnet_type = _choice(data.get("subnet_type"), f"{label}: subnet_type", SUBNET_TYPES, "private")
        positions = schema.strings(data.get("subnet_positions"), f"{label}: subnet_positions")
        for position in positions:
            _choice(position, f"{label}: subnet_positions", ORDINALS, None)
        if len(set(positions)) != len(positions):
            raise ValueError(f"{label}: subnet_positions repeats a position.")
        if positions and len(positions) < 2:
            raise ValueError(f"{label}: a DB subnet group needs at least two subnet_positions.")
        security_groups = schema.strings(
            data.get("security_groups"), f"{label}: security_groups", allow_empty=False
        )
        if not security_groups:
            raise ValueError(f"{label}: security_groups needs at least one group.")
        publicly_accessible = schema.boolean(
            data.get("publicly_accessible"), f"{label}: publicly_accessible"
        )
        if publicly_accessible and subnet_type != "public":
            raise ValueError(f"{label}: publicly_accessible requires subnet_type: public.")
        multi_az = schema.boolean(data.get("multi_az"), f"{label}: multi_az")
        availability_zone = schema.string(data.get("availability_zone"), f"{label}: availability_zone")
        if multi_az and availability_zone:
            raise ValueError(f"{label}: availability_zone cannot be set with multi_az.")

        parameter_group_name = schema.string(
            data.get("parameter_group_name"), f"{label}: parameter_group_name"
        )
        parameters = schema.string_map(data.get("parameters"), f"{label}: parameters")
        family = schema.string(data.get("parameter_group_family"), f"{label}: parameter_group_family")
        if parameter_group_name and (parameters or family):
            raise ValueError(
                f"{label}: use parameter_group_name for an existing group, or "
                "parameters/parameter_group_family for a new one, not both."
            )

        backup_retention = schema.integer(
            data.get("backup_retention_period"), f"{label}: backup_retention_period",
            default=7, minimum=0, maximum=35,
        )
        backup_window = _pattern(
            data.get("backup_window"), f"{label}: backup_window", WINDOW, "hh:mm-hh:mm"
        )
        maintenance_window = _pattern(
            data.get("maintenance_window"), f"{label}: maintenance_window", WINDOW,
            "ddd:hh:mm-ddd:hh:mm",
        )
        removal_policy = _choice(
            data.get("removal_policy"), f"{label}: removal_policy", tuple(REMOVAL_POLICIES), "retain"
        )
        monitoring_interval = _choice(
            data.get("monitoring_interval"), f"{label}: monitoring_interval", MONITORING_INTERVALS, 0
        )
        monitoring_role_arn = _pattern(
            data.get("monitoring_role_arn"), f"{label}: monitoring_role_arn", ROLE_ARN,
            "an IAM role ARN",
        )
        if monitoring_role_arn and not monitoring_interval:
            raise ValueError(f"{label}: monitoring_role_arn requires monitoring_interval > 0.")
        insights = schema.boolean(
            data.get("performance_insights_enabled"), f"{label}: performance_insights_enabled"
        )
        insights_key = _pattern(
            data.get("performance_insights_kms_key_arn"),
            f"{label}: performance_insights_kms_key_arn", KMS_ARN, kms_hint,
        )
        insights_retention = _choice(
            data.get("performance_insights_retention_period"),
            f"{label}: performance_insights_retention_period", PERFORMANCE_INSIGHTS_RETENTION, None,
        )
        if (insights_key or insights_retention) and not insights:
            raise ValueError(
                f"{label}: performance insights options require performance_insights_enabled: true."
            )
        create_read_replica = schema.boolean(
            data.get("create_read_replica"), f"{label}: create_read_replica"
        )
        if replica_class and not create_read_replica:
            raise ValueError(f"{label}: replica_instance_class requires create_read_replica: true.")
        if create_read_replica and backup_retention == 0:
            raise ValueError(f"{label}: read replicas need backup_retention_period above 0.")

        return cls(
            name=name,
            create=schema.boolean(data.get("create"), f"{label}: create", default=True),
            engine=engine,
            engine_version=engine_version,
            engine_major_version=schema.string(
                data.get("engine_major_version"), f"{label}: engine_major_version"
            ),
            instance_class=instance_class,
            allocated_storage=allocated,
            max_allocated_storage=maximum,
            storage_type=storage_type,
            storage_encrypted=storage_encrypted,
            kms_key_arn=kms_key_arn,
            iops=iops,
            database_name=database_name,
            master_username=username,
            password_management=password_management,
            master_user_secret=master_user_secret,
            secrets_kms_key_arn=secrets_kms,
            port=schema.integer(data.get("port"), f"{label}: port", minimum=1150, maximum=65535),
            subnet_type=subnet_type,
            subnet_positions=positions,
            security_groups=security_groups,
            publicly_accessible=publicly_accessible,
            multi_az=multi_az,
            availability_zone=availability_zone,
            parameter_group_name=parameter_group_name,
            parameter_group_family=family,
            parameters=parameters,
            option_group_name=schema.string(
                data.get("option_group_name"), f"{label}: option_group_name"
            ),
            backup_retention_period=backup_retention,
            backup_window=backup_window,
            maintenance_window=maintenance_window,
            auto_minor_version_upgrade=schema.boolean(
                data.get("auto_minor_version_upgrade"), f"{label}: auto_minor_version_upgrade",
                default=True,
            ),
            deletion_protection=schema.boolean(
                data.get("deletion_protection"), f"{label}: deletion_protection", default=True
            ),
            removal_policy=removal_policy,
            copy_tags_to_snapshot=schema.boolean(
                data.get("copy_tags_to_snapshot"), f"{label}: copy_tags_to_snapshot", default=True
            ),
            enabled_cloudwatch_logs_exports=schema.strings(
                data.get("enabled_cloudwatch_logs_exports"),
                f"{label}: enabled_cloudwatch_logs_exports",
            ),
            monitoring_interval=monitoring_interval,
            monitoring_role_arn=monitoring_role_arn,
            performance_insights_enabled=insights,
            performance_insights_kms_key_arn=insights_key,
            performance_insights_retention_period=insights_retention,
            apply_immediately=schema.boolean(
                data.get("apply_immediately"), f"{label}: apply_immediately"
            ),
            create_read_replica=create_read_replica,
            replica_instance_class=replica_class,
            tags=schema.string_map(data.get("tags"), f"{label}: tags"),
        )


@dataclass(frozen=True)
class RdsProps:
    instances: tuple[RdsInstanceProps, ...] = ()

    def __bool__(self) -> bool:
        return any(instance.create for instance in self.instances)

    @property
    def enabled(self) -> tuple[RdsInstanceProps, ...]:
        return tuple(instance for instance in self.instances if instance.create)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> RdsProps:
        del base_dir  # No file-backed documents in this family.
        return cls(schema.items(raw, "rds_instances", RdsInstanceProps.from_dict))


def _removal(policy: str) -> cdk.RemovalPolicy:
    return REMOVAL_POLICIES[policy]


def _helper_removal(policy: str) -> cdk.RemovalPolicy:
    """Groups and secrets a retained database still uses must outlive the stack."""
    return cdk.RemovalPolicy.DESTROY if policy == "destroy" else cdk.RemovalPolicy.RETAIN


class RdsInstances(Construct):
    """One `rds.DatabaseInstance` per enabled config entry."""

    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        common: Common,
        prefix: str,
        props: RdsProps,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
    ) -> None:
        super().__init__(scope, construct_id_)
        self.instances: dict[str, rds.DatabaseInstance] = {}
        self.replicas: dict[str, rds.DatabaseInstanceReadReplica] = {}
        self.secrets: dict[str, secretsmanager.ISecret] = {}
        self.subnet_groups: dict[str, rds.SubnetGroup] = {}
        secrets = secrets or {}

        for entry in props.enabled:
            identifier = common.name(prefix, entry.name)
            candidates = [identifier] + ([f"{identifier}-replica"] if entry.create_read_replica else [])
            for candidate in candidates:
                if len(candidate) > 63 or not IDENTIFIER.fullmatch(candidate):
                    raise ValueError(
                        f"RDS instance '{entry.name}': identifier '{candidate}' must be at most "
                        "63 lowercase letters, digits or single hyphens, starting with a letter."
                    )
            self._add(entry, identifier, common, prefix, network, security_groups, secrets)

    def _add(
        self,
        entry: RdsInstanceProps,
        identifier: str,
        common: Common,
        prefix: str,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        secrets: Mapping[str, secretsmanager.ISecret],
    ) -> None:
        label = f"RDS instance '{entry.name}'"
        cid = construct_id(entry.name)
        subnets = resolve_subnets(network, entry.subnet_type, entry.subnet_positions, label)
        if len({subnet.availability_zone for subnet in subnets}) < 2:
            raise ValueError(f"{label}: a DB subnet group needs subnets in at least two AZs.")
        if entry.availability_zone and entry.availability_zone not in {
            subnet.availability_zone for subnet in subnets
        }:
            raise ValueError(
                f"{label}: availability_zone '{entry.availability_zone}' is not one of the "
                "selected subnets' zones."
            )
        groups = resolve_security_groups(self, security_groups, entry.security_groups, label)
        helper_removal = _helper_removal(entry.removal_policy)

        subnet_group = rds.SubnetGroup(
            self,
            f"{cid}SubnetGroup",
            description=f"Subnet group for {identifier}",
            vpc=network.vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=subnets),
            subnet_group_name=common.name(prefix, entry.name, "subnet-group"),
            removal_policy=helper_removal,
        )
        cdk.Tags.of(subnet_group).add("Name", common.name(prefix, entry.name, "subnet-group"))
        self.subnet_groups[entry.name] = subnet_group

        factory, versions, _ = ENGINES[entry.engine]
        engine = factory(version=versions.of(entry.engine_version, entry.major_version))

        parameter_group = None
        if entry.parameter_group_name:
            parameter_group = rds.ParameterGroup.from_parameter_group_name(
                self, f"{cid}ParameterGroupRef", entry.parameter_group_name
            )
        elif entry.parameters or entry.parameter_group_family:
            family = entry.parameter_group_family or engine.parameter_group_family
            if not family:
                raise ValueError(f"{label}: set parameter_group_family for engine '{entry.engine}'.")
            group_name = common.name(prefix, entry.name, "parameter-group")
            cfn_group = rds.CfnDBParameterGroup(
                self,
                f"{cid}ParameterGroup",
                db_parameter_group_name=group_name,
                family=family,
                description=f"Custom parameter group for {identifier}",
                parameters=dict(entry.parameters) or None,
            )
            cfn_group.apply_removal_policy(helper_removal)
            cdk.Tags.of(cfn_group).add("Name", group_name)
            parameter_group = rds.ParameterGroup.from_parameter_group_name(
                self, f"{cid}ParameterGroupRef", cfn_group.ref
            )

        option_group = (
            rds.OptionGroup.from_option_group_name(self, f"{cid}OptionGroup", entry.option_group_name)
            if entry.option_group_name else None
        )

        def key(arn: str | None, suffix: str) -> kms.IKey | None:
            return kms.Key.from_key_arn(self, f"{cid}{suffix}", arn) if arn else None

        secret_key = key(entry.secrets_kms_key_arn, "SecretKey")
        manage_password = None
        if entry.master_user_secret:
            secret = schema.lookup(secrets, entry.master_user_secret, f"{label} master_user_secret")
            # Import locally so the RDS attachment stays in this stack instead of
            # making the secrets stack depend back on the database.
            secret = secretsmanager.Secret.from_secret_complete_arn(
                self, f"{cid}MasterSecret", secret.secret_arn
            )
            credentials = rds.Credentials.from_secret(secret)
        elif entry.password_management == "rds_managed":
            manage_password = True
            credentials = rds.Credentials.from_username(
                entry.master_username, encryption_key=secret_key
            )
        else:
            credentials = rds.Credentials.from_generated_secret(
                entry.master_username,
                secret_name=f"{identifier}/rds-credentials",
                encryption_key=secret_key,
            )

        instance = rds.DatabaseInstance(
            self,
            cid,
            instance_identifier=identifier,
            engine=engine,
            instance_type=ec2.InstanceType(entry.instance_class.removeprefix("db.")),
            vpc=network.vpc,
            subnet_group=subnet_group,
            security_groups=groups,
            credentials=credentials,
            manage_master_user_password=manage_password,
            database_name=entry.database_name,
            port=entry.port,
            allocated_storage=entry.allocated_storage,
            max_allocated_storage=entry.max_allocated_storage,
            storage_type=getattr(rds.StorageType, entry.storage_type.upper()),
            storage_encrypted=entry.storage_encrypted,
            storage_encryption_key=key(entry.kms_key_arn, "StorageKey"),
            iops=entry.iops,
            publicly_accessible=entry.publicly_accessible,
            multi_az=entry.multi_az,
            availability_zone=entry.availability_zone,
            parameter_group=parameter_group,
            option_group=option_group,
            backup_retention=cdk.Duration.days(entry.backup_retention_period),
            preferred_backup_window=entry.backup_window,
            preferred_maintenance_window=entry.maintenance_window,
            auto_minor_version_upgrade=entry.auto_minor_version_upgrade,
            deletion_protection=entry.deletion_protection,
            removal_policy=_removal(entry.removal_policy),
            copy_tags_to_snapshot=entry.copy_tags_to_snapshot,
            cloudwatch_logs_exports=list(entry.enabled_cloudwatch_logs_exports) or None,
            monitoring_interval=(
                cdk.Duration.seconds(entry.monitoring_interval) if entry.monitoring_interval else None
            ),
            monitoring_role=(
                iam.Role.from_role_arn(self, f"{cid}MonitoringRole", entry.monitoring_role_arn)
                if entry.monitoring_role_arn else None
            ),
            enable_performance_insights=entry.performance_insights_enabled or None,
            performance_insight_encryption_key=key(
                entry.performance_insights_kms_key_arn, "InsightsKey"
            ),
            apply_immediately=entry.apply_immediately or None,
        )
        cfn_instance = instance.node.default_child
        if entry.performance_insights_retention_period and isinstance(cfn_instance, rds.CfnDBInstance):
            cfn_instance.performance_insights_retention_period = (
                entry.performance_insights_retention_period
            )
        generated_secret = instance.node.try_find_child("Secret")
        if isinstance(generated_secret, secretsmanager.Secret):
            generated_secret.apply_removal_policy(helper_removal)
        self._tag(instance, identifier, entry.tags)
        self.instances[entry.name] = instance
        if instance.secret is not None:
            self.secrets[entry.name] = instance.secret

        if entry.create_read_replica:
            replica = rds.DatabaseInstanceReadReplica(
                self,
                f"{cid}Replica",
                instance_identifier=f"{identifier}-replica",
                source_database_instance=instance,
                instance_type=ec2.InstanceType(
                    (entry.replica_instance_class or entry.instance_class).removeprefix("db.")
                ),
                vpc=network.vpc,
                subnet_group=subnet_group,
                security_groups=groups,
                publicly_accessible=entry.publicly_accessible,
                auto_minor_version_upgrade=entry.auto_minor_version_upgrade,
                storage_encrypted=entry.storage_encrypted,
                storage_encryption_key=key(entry.kms_key_arn, "ReplicaStorageKey"),
                deletion_protection=entry.deletion_protection,
                enable_performance_insights=entry.performance_insights_enabled or None,
                performance_insight_encryption_key=key(
                    entry.performance_insights_kms_key_arn, "ReplicaInsightsKey"
                ),
                # A replica has no final snapshot of its own; its data lives in the source.
                removal_policy=helper_removal,
            )
            self._tag(replica, f"{identifier}-replica", entry.tags)
            self.replicas[entry.name] = replica

    @staticmethod
    def _tag(resource: Construct, name: str, tags: Mapping[str, str]) -> None:
        cdk.Tags.of(resource).add("Name", name)
        for tag_key, value in tags.items():
            cdk.Tags.of(resource).add(tag_key, value)
