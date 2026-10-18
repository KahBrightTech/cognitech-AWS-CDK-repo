"""AWS Backup vaults, plans and selections."""

from __future__ import annotations

import re

from aws_cdk import aws_backup as backup
from aws_cdk import aws_iam as iam
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.backup import BackupProps, Backups
from cognitech_cdk.stacks.base import EnvironmentStack


def _logical(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name) if part)


class BackupStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: BackupProps,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        built = Backups(self, "Backups", props=props, naming=self.resource_name)
        self.vaults: dict[str, backup.IBackupVault] = dict(built.vaults)
        self.plans: dict[str, backup.IBackupPlan] = dict(built.plans)
        self.roles: dict[str, iam.IRole] = dict(built.roles)
        self.selections: dict[str, backup.BackupSelection] = dict(built.selections)

        for name in built.vaults:
            logical = _logical(name)
            vault, plan, role = built.vaults[name], built.plans[name], built.roles[name]
            self.output(f"{logical}VaultName", vault.backup_vault_name, f"{name}-backup-vault-name")
            self.output(f"{logical}VaultArn", vault.backup_vault_arn, f"{name}-backup-vault-arn")
            self.output(f"{logical}PlanId", plan.backup_plan_id, f"{name}-backup-plan-id")
            self.output(f"{logical}PlanArn", plan.backup_plan_arn, f"{name}-backup-plan-arn")
            self.output(f"{logical}RoleArn", role.role_arn, f"{name}-backup-role-arn")
            if name in built.selections:
                self.output(
                    f"{logical}SelectionId", built.selections[name].selection_id, f"{name}-backup-selection-id"
                )
