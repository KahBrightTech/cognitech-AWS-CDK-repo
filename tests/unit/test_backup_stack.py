"""AWS Backup vaults, plans, rules and selections."""

from __future__ import annotations

import copy

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.backup import BackupProps
from cognitech_cdk.stacks.backup_stack import BackupStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

DAILY = {
    "name": "daily",
    "plan": {
        "name": "daily-plan",
        "rules": [{
            "rule_name": "daily",
            "schedule": "cron(0 5 ? * * *)",
            "start_window": 60,
            "completion_window": 180,
            "lifecycle": {"cold_storage_after_days": 30, "delete_after_days": 120},
            "copy_actions": [{
                "destination_vault_arn": "arn:aws:backup:us-west-2:111122223333:backup-vault:dr",
                "lifecycle": {"delete_after_days": 35},
            }],
        }],
        "selection": {
            "selection_name": "tagged",
            "selection_tags": [{"type": "STRINGEQUALS", "key": "Backup", "value": "true"}],
            "resources": ["arn:aws:ec2:us-east-1:111122223333:volume/vol-0123456789abcdef0"],
        },
    },
}


def _with(**changes):
    item = copy.deepcopy(DAILY)
    for path, value in changes.items():
        target = item
        *parents, last = path.split("__")
        for parent in parents:
            target = target[parent] if not parent.isdigit() else target[int(parent)]
        target[last] = value
    return item


def _stack(write_env, raw) -> tuple[BackupStack, Template]:
    environment = load_environment("test", root=write_env(NETWORK))
    app = cdk.App()
    stack = BackupStack(
        app, "backup", environment=environment, props=BackupProps.from_dict(raw),
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    return stack, Template.from_stack(stack)


# --- parsing ---------------------------------------------------------------


def test_none_is_empty():
    assert not BackupProps.from_dict(None)


@pytest.mark.parametrize(("item", "message"), [
    (_with(plan__rules__0__schedule="rate(1 day)"), "must be a cron"),
    (_with(plan__rules__0__start_window=30), "start_window must be at least 60"),
    (_with(plan__rules__0__completion_window=90), "at least 60 minutes more than start_window"),
    (_with(plan__rules__0__lifecycle={"cold_storage_after_days": 30, "delete_after_days": 60}),
     "at least 90 days after"),
    (_with(plan__rules__0__copy_actions=[{"destination_vault_arn": "vault"}]), "backup vault ARN"),
    (_with(plan__rules=[]), "rules must be a nonempty list"),
    (_with(plan__selection={"selection_name": "empty"}), "set selection_tags or resources"),
    (_with(plan__selection__selection_tags=[{"type": "STRINGLIKE", "key": "a", "value": "b"}]),
     "type must be STRINGEQUALS"),
    (_with(kms_key_id="alias/aws/backup"), "must be a KMS key ARN"),
    (_with(extra=True), "unknown fields: extra"),
])
def test_invalid_config(item, message):
    with pytest.raises(ValueError, match=message):
        BackupProps.from_dict([item])


# --- synthesized resources -------------------------------------------------


def test_vault_plan_rules_and_selection(write_env):
    stack, template = _stack(write_env, [DAILY])

    template.has_resource("AWS::Backup::BackupVault", {
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": {
            "BackupVaultName": "int-production-use1-uat-daily-vault",
            "BackupVaultTags": {"Name": "int-production-use1-uat-daily-vault", "Owner": "someone@example.com"},
        },
    })
    vault_id = next(iter(template.find_resources("AWS::Backup::BackupVault")))
    template.has_resource_properties("AWS::Backup::BackupPlan", {
        "BackupPlan": {
            "BackupPlanName": "int-production-use1-uat-daily-plan",
            "BackupPlanRule": [{
                "RuleName": "daily",
                "TargetBackupVault": {"Fn::GetAtt": [vault_id, "BackupVaultName"]},
                "ScheduleExpression": "cron(0 5 ? * * *)",
                "StartWindowMinutes": 60,
                "CompletionWindowMinutes": 180,
                "Lifecycle": {"MoveToColdStorageAfterDays": 30, "DeleteAfterDays": 120},
                "CopyActions": [{
                    "DestinationBackupVaultArn": "arn:aws:backup:us-west-2:111122223333:backup-vault:dr",
                    "Lifecycle": {"DeleteAfterDays": 35},
                }],
            }],
        },
    })
    role_id = next(iter(template.find_resources("AWS::IAM::Role")))
    template.has_resource_properties("AWS::IAM::Role", {
        "RoleName": "int-production-use1-uat-daily-backup-role",
        "AssumeRolePolicyDocument": {"Statement": [Match.object_like({
            "Principal": {"Service": "backup.amazonaws.com"}, "Action": "sts:AssumeRole",
        })]},
        "ManagedPolicyArns": [{"Fn::Join": ["", [
            "arn:", {"Ref": "AWS::Partition"}, ":iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
        ]]}],
    })
    template.has_resource_properties("AWS::Backup::BackupSelection", {
        "BackupSelection": {
            "SelectionName": "tagged",
            "IamRoleArn": {"Fn::GetAtt": [role_id, "Arn"]},
            "ListOfTags": [{"ConditionType": "STRINGEQUALS", "ConditionKey": "Backup", "ConditionValue": "true"}],
            "Resources": ["arn:aws:ec2:us-east-1:111122223333:volume/vol-0123456789abcdef0"],
        },
    })
    assert set(stack.vaults) == set(stack.plans) == {"daily"}

    outputs = template.find_outputs("*")
    for logical, suffix in {
        "DailyVaultName": "daily-backup-vault-name",
        "DailyVaultArn": "daily-backup-vault-arn",
        "DailyPlanId": "daily-backup-plan-id",
        "DailyPlanArn": "daily-backup-plan-arn",
        "DailyRoleArn": "daily-backup-role-arn",
        "DailySelectionId": "daily-backup-selection-id",
    }.items():
        assert outputs[logical]["Export"]["Name"] == f"backup-{suffix}"


def test_minimal_backup_without_selection(write_env):
    item = {"name": "weekly", "role_name": "custom-backup-role", "removal_policy": "destroy",
            "kms_key_id": "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "plan": {"name": "weekly", "rules": [{"rule_name": "weekly", "schedule": "cron(0 5 ? * 1 *)"}]}}
    stack, template = _stack(write_env, [item])
    template.has_resource("AWS::Backup::BackupVault", {
        "DeletionPolicy": "Delete",
        "Properties": {"EncryptionKeyArn": item["kms_key_id"]},
    })
    template.has_resource_properties("AWS::IAM::Role", {"RoleName": "custom-backup-role"})
    template.resource_count_is("AWS::Backup::BackupSelection", 0)
    plan = next(iter(template.find_resources("AWS::Backup::BackupPlan").values()))
    rule = plan["Properties"]["BackupPlan"]["BackupPlanRule"][0]
    assert "Lifecycle" not in rule and "CopyActions" not in rule
    assert stack.selections == {}
    assert "WeeklySelectionId" not in template.find_outputs("*")
