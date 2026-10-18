"""AWS Backup vaults, plans, rules and selections (Terraform `AWSBackup` module)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import aws_cdk as cdk
from aws_cdk import aws_backup as backup
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.constructs.secrets import removal_policy

BACKUP_NAME = re.compile(r"[A-Za-z0-9_.-]{1,50}")
CRON = re.compile(r"cron\(.+\)")
KMS_ARN = re.compile(r"arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key/.+")
VAULT_ARN = re.compile(r"arn:aws[a-z-]*:backup:[a-z0-9-]+:\d{12}:backup-vault:.+")
SERVICE_POLICY = "service-role/AWSBackupServiceRolePolicyForBackup"


def _backup_name(value: Any, label: str) -> str:
    text = schema.required_string(value, label)
    if not BACKUP_NAME.fullmatch(text):
        raise ValueError(f"{label} must be 1-50 letters, digits, '-', '_' or '.'.")
    return text


@dataclass(frozen=True)
class LifecycleProps:
    cold_storage_after_days: int | None = None
    delete_after_days: int | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "LifecycleProps":
        if raw is None:
            return cls()
        raw = schema.mapping(raw, label, {"cold_storage_after_days", "delete_after_days"})
        cold = schema.integer(raw.get("cold_storage_after_days"), f"{label}.cold_storage_after_days", minimum=1)
        delete = schema.integer(raw.get("delete_after_days"), f"{label}.delete_after_days", minimum=1)
        if cold is not None and delete is not None and delete < cold + 90:
            raise ValueError(
                f"{label}: delete_after_days must be at least 90 days after cold_storage_after_days."
            )
        return cls(cold_storage_after_days=cold, delete_after_days=delete)

    def durations(self) -> dict[str, cdk.Duration | None]:
        return {
            "delete_after": cdk.Duration.days(self.delete_after_days) if self.delete_after_days else None,
            "move_to_cold_storage_after": cdk.Duration.days(self.cold_storage_after_days)
            if self.cold_storage_after_days else None,
        }


@dataclass(frozen=True)
class CopyActionProps:
    destination_vault_arn: str
    lifecycle: LifecycleProps = LifecycleProps()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "CopyActionProps":
        raw = schema.mapping(raw, label, {"destination_vault_arn", "lifecycle"}, required=("destination_vault_arn",))
        arn = schema.required_string(raw["destination_vault_arn"], f"{label}.destination_vault_arn")
        if not VAULT_ARN.fullmatch(arn):
            raise ValueError(f"{label}.destination_vault_arn must be a backup vault ARN.")
        return cls(destination_vault_arn=arn, lifecycle=LifecycleProps.from_dict(raw.get("lifecycle"), f"{label}.lifecycle"))


@dataclass(frozen=True)
class RuleProps:
    rule_name: str
    schedule: str
    start_window: int | None = None
    completion_window: int | None = None
    lifecycle: LifecycleProps = LifecycleProps()
    copy_actions: tuple[CopyActionProps, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "RuleProps":
        raw = schema.mapping(raw, label, {
            "rule_name", "schedule", "start_window", "completion_window", "lifecycle", "copy_actions",
        }, required=("rule_name", "schedule"))
        rule_name = _backup_name(raw["rule_name"], f"{label}.rule_name")
        label = f"{label} '{rule_name}'"
        schedule = schema.required_string(raw["schedule"], f"{label}.schedule")
        if not CRON.fullmatch(schedule):
            raise ValueError(f"{label}.schedule must be a cron(...) expression.")
        start = schema.integer(raw.get("start_window"), f"{label}.start_window", minimum=60)
        completion = schema.integer(raw.get("completion_window"), f"{label}.completion_window", minimum=60)
        if start is not None and completion is not None and completion < start + 60:
            raise ValueError(f"{label}: completion_window must be at least 60 minutes more than start_window.")
        copy_actions = raw.get("copy_actions")
        if copy_actions is not None and not isinstance(copy_actions, list):
            raise ValueError(f"{label}.copy_actions must be a list.")
        return cls(
            rule_name=rule_name,
            schedule=schedule,
            start_window=start,
            completion_window=completion,
            lifecycle=LifecycleProps.from_dict(raw.get("lifecycle"), f"{label}.lifecycle"),
            copy_actions=tuple(
                CopyActionProps.from_dict(item, f"{label}.copy_actions[{index}]")
                for index, item in enumerate(copy_actions or [])
            ),
        )


@dataclass(frozen=True)
class SelectionTagProps:
    key: str
    value: str

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "SelectionTagProps":
        raw = schema.mapping(raw, label, {"type", "key", "value"}, required=("key", "value"))
        if raw.get("type", "STRINGEQUALS") != "STRINGEQUALS":
            raise ValueError(f"{label}.type must be STRINGEQUALS.")
        return cls(
            key=schema.required_string(raw["key"], f"{label}.key"),
            value=schema.required_string(raw["value"], f"{label}.value"),
        )


@dataclass(frozen=True)
class SelectionProps:
    selection_name: str
    selection_tags: tuple[SelectionTagProps, ...] = ()
    resources: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "SelectionProps | None":
        if raw is None:
            return None
        raw = schema.mapping(raw, label, {"selection_name", "selection_tags", "resources"}, required=("selection_name",))
        tags = raw.get("selection_tags")
        if tags is not None and not isinstance(tags, list):
            raise ValueError(f"{label}.selection_tags must be a list.")
        resources = schema.strings(raw.get("resources"), f"{label}.resources")
        bad = [arn for arn in resources if not arn.startswith("arn:")]
        if bad:
            raise ValueError(f"{label}.resources must be ARNs: {', '.join(bad)}.")
        parsed_tags = tuple(
            SelectionTagProps.from_dict(item, f"{label}.selection_tags[{index}]")
            for index, item in enumerate(tags or [])
        )
        if not parsed_tags and not resources:
            raise ValueError(f"{label}: set selection_tags or resources.")
        return cls(
            selection_name=_backup_name(raw["selection_name"], f"{label}.selection_name"),
            selection_tags=parsed_tags,
            resources=resources,
        )


@dataclass(frozen=True)
class BackupPlanProps:
    name: str
    rules: tuple[RuleProps, ...]
    selection: SelectionProps | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "BackupPlanProps":
        raw = schema.mapping(raw, label, {"name", "rules", "selection"}, required=("name", "rules"))
        rules = raw["rules"]
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"{label}.rules must be a nonempty list.")
        parsed = tuple(RuleProps.from_dict(rule, f"{label}.rules[{index}]") for index, rule in enumerate(rules))
        names = [rule.rule_name.casefold() for rule in parsed]
        if len(set(names)) != len(names):
            raise ValueError(f"{label}.rules: rule_name values must be unique.")
        return cls(
            name=schema.name(raw["name"], f"{label}.name"),
            rules=parsed,
            selection=SelectionProps.from_dict(raw.get("selection"), f"{label}.selection"),
        )


@dataclass(frozen=True)
class BackupVaultProps:
    name: str
    plan: BackupPlanProps
    kms_key_arn: str | None = None
    role_name: str | None = None
    removal_policy: cdk.RemovalPolicy = cdk.RemovalPolicy.RETAIN

    @classmethod
    def from_dict(cls, raw: Any) -> "BackupVaultProps":
        raw = schema.mapping(raw, "backups item", {
            "name", "kms_key_id", "role_name", "plan", "removal_policy",
        }, required=("name", "plan"))
        name = schema.name(raw["name"], "backups item name")
        label = f"backups '{name}'"
        kms_key_arn = schema.string(raw.get("kms_key_id"), f"{label}.kms_key_id")
        if kms_key_arn is not None and not KMS_ARN.fullmatch(kms_key_arn):
            raise ValueError(f"{label}.kms_key_id must be a KMS key ARN.")
        role_name = raw.get("role_name")
        return cls(
            name=name,
            plan=BackupPlanProps.from_dict(raw["plan"], f"{label}.plan"),
            kms_key_arn=kms_key_arn,
            role_name=schema.name(role_name, f"{label}.role_name") if role_name is not None else None,
            removal_policy=removal_policy(raw.get("removal_policy"), f"{label}.removal_policy"),
        )


@dataclass(frozen=True)
class BackupProps:
    backups: tuple[BackupVaultProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.backups)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Any = None) -> "BackupProps":
        return cls(backups=schema.items(raw, "backups", BackupVaultProps.from_dict))


class Backups(Construct):
    """Per item: service role, vault, plan with rules and an optional selection."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        props: BackupProps,
        naming: Callable[..., str],
    ) -> None:
        super().__init__(scope, construct_id)
        self.roles: dict[str, iam.Role] = {}
        self.vaults: dict[str, backup.BackupVault] = {}
        self.plans: dict[str, backup.BackupPlan] = {}
        self.selections: dict[str, backup.BackupSelection] = {}

        for item in props.backups:
            scope_ = Construct(self, item.name)
            role_name = item.role_name or naming(item.name, "backup-role")
            role = iam.Role(
                scope_,
                "Role",
                role_name=role_name,
                assumed_by=iam.ServicePrincipal("backup.amazonaws.com"),
                managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(SERVICE_POLICY)],
            )
            cdk.Tags.of(role).add("Name", role_name)

            vault_name = naming(item.name, "vault")
            vault = backup.BackupVault(
                scope_,
                "Vault",
                backup_vault_name=vault_name,
                encryption_key=kms.Key.from_key_arn(scope_, "VaultKey", item.kms_key_arn)
                if item.kms_key_arn else None,
                removal_policy=item.removal_policy,
            )
            cdk.Tags.of(vault).add("Name", vault_name)

            plan_name = naming(item.plan.name)
            plan = backup.BackupPlan(scope_, "Plan", backup_plan_name=plan_name, backup_vault=vault)
            cdk.Tags.of(plan).add("Name", plan_name)
            for index, rule in enumerate(item.plan.rules):
                plan.add_rule(backup.BackupPlanRule(
                    rule_name=rule.rule_name,
                    backup_vault=vault,
                    schedule_expression=events.Schedule.expression(rule.schedule),
                    start_window=cdk.Duration.minutes(rule.start_window) if rule.start_window else None,
                    completion_window=cdk.Duration.minutes(rule.completion_window)
                    if rule.completion_window else None,
                    copy_actions=[
                        backup.BackupPlanCopyActionProps(
                            destination_backup_vault=backup.BackupVault.from_backup_vault_arn(
                                scope_, f"CopyVault{index}x{copy_index}", action.destination_vault_arn
                            ),
                            **action.lifecycle.durations(),
                        )
                        for copy_index, action in enumerate(rule.copy_actions)
                    ] or None,
                    **rule.lifecycle.durations(),
                ))

            if item.plan.selection is not None:
                selection = item.plan.selection
                self.selections[item.name] = plan.add_selection(
                    "Selection",
                    backup_selection_name=selection.selection_name,
                    role=role,
                    disable_default_backup_policy=True,
                    resources=[
                        *(backup.BackupResource.from_tag(tag.key, tag.value, backup.TagOperation.STRING_EQUALS)
                          for tag in selection.selection_tags),
                        *(backup.BackupResource.from_arn(arn) for arn in selection.resources),
                    ],
                )

            self.roles[item.name] = role
            self.vaults[item.name] = vault
            self.plans[item.name] = plan
