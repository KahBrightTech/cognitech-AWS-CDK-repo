"""RDS parsing, credentials, placement, safety defaults, names and outputs."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.rds import RdsProps
from cognitech_cdk.stacks.network_stack import NetworkStack
from cognitech_cdk.stacks.rds_stack import RdsStack
from cognitech_cdk.stacks.security_stack import SecurityStack

NETWORK = """
name: test
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: true
"""
SECURITY_GROUPS = """
- name: db
  description: databases
"""
RDS = """
- name: app-postgres
  engine: postgres
  engine_version: "15.16"
  instance_class: db.t3.micro
  allocated_storage: 20
  max_allocated_storage: 40
  database_name: ecsdb
  port: 5432
  security_groups: [db]
"""


def _props(text: str = RDS) -> RdsProps:
    return RdsProps.from_dict(yaml.safe_load(text))


def _stacks(write_env, text: str = RDS, *, secrets=None, extra_secret=False):
    environment = load_environment(
        "test", root=write_env(NETWORK, security_groups=SECURITY_GROUPS)
    )
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network = NetworkStack(app, "test-network", environment=environment, env=env)
    security = SecurityStack(
        app, "test-security", environment=environment, vpc=network.network.vpc, env=env
    )
    if extra_secret:
        secret_stack = cdk.Stack(app, "test-secrets", env=env)
        secrets = {"shared-db": secretsmanager.Secret(secret_stack, "Db")}
    stack = RdsStack(
        app,
        "test-rds",
        environment=environment,
        props=_props(text),
        network=network.network,
        security_groups=security.security_groups.groups,
        secrets=secrets,
        env=env,
    )
    return stack


def test_empty_config_creates_nothing():
    assert not RdsProps.from_dict(None)
    assert not RdsProps.from_dict([])
    assert not _props(RDS + "  create: false\n")
    assert _props()


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"surprise": 1}, "unknown fields: surprise"),
        ({"engine": "db2"}, "engine must be one of"),
        ({"instance_class": "t3.micro"}, "RDS class"),
        ({"security_groups": []}, "security_groups"),
        ({"publicly_accessible": True}, "requires subnet_type: public"),
        ({"multi_az": True, "availability_zone": "us-east-1a"}, "cannot be set with multi_az"),
        ({"removal_policy": "keep"}, "removal_policy must be one of"),
        ({"max_allocated_storage": 10}, "at least allocated_storage"),
        ({"storage_type": "io1"}, "requires iops"),
        ({"master_password": "hunter2"}, "unknown fields: master_password"),
        ({"master_user_secret": "s", "master_username": "x"}, "leave out"),
        ({"parameter_group_name": "existing", "parameters": {"a": "b"}}, "not both"),
        ({"performance_insights_retention_period": 7}, "performance_insights_enabled"),
        ({"subnet_positions": ["primary"]}, "at least two"),
    ],
)
def test_validation(patch, message):
    raw = yaml.safe_load(RDS)
    raw[0].update(patch)
    with pytest.raises(ValueError, match=message):
        RdsProps.from_dict(raw)


def test_duplicate_names_rejected():
    raw = yaml.safe_load(RDS) * 2
    with pytest.raises(ValueError, match="duplicate name"):
        RdsProps.from_dict(raw)


def test_instance_is_private_encrypted_and_protected(write_env):
    template = Template.from_stack(_stacks(write_env))
    template.resource_count_is("AWS::RDS::DBInstance", 1)
    template.has_resource(
        "AWS::RDS::DBInstance",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": Match.object_like({
                "DBInstanceIdentifier": "int-production-use1-test-app-postgres",
                "Engine": "postgres",
                "EngineVersion": "15.16",
                "DBInstanceClass": "db.t3.micro",
                "AllocatedStorage": "20",
                "MaxAllocatedStorage": 40,
                "StorageType": "gp3",
                "StorageEncrypted": True,
                "DBName": "ecsdb",
                "Port": "5432",
                "PubliclyAccessible": False,
                "DeletionProtection": True,
                "BackupRetentionPeriod": 7,
                "CopyTagsToSnapshot": True,
                "AutoMinorVersionUpgrade": True,
                "MultiAZ": False,
            }),
        },
    )
    template.has_resource_properties(
        "AWS::RDS::DBSubnetGroup",
        {
            "DBSubnetGroupName": "int-production-use1-test-app-postgres-subnet-group",
            "SubnetIds": Match.array_with([
                {"Fn::ImportValue": Match.string_like_regexp("PrivateSubnetPrimary")},
            ]),
        },
    )
    template.has_resource("AWS::RDS::DBSubnetGroup", {"DeletionPolicy": "Retain"})


def test_master_password_is_generated_never_plaintext(write_env):
    stack = _stacks(write_env)
    template = Template.from_stack(stack)
    template.has_resource(
        "AWS::SecretsManager::Secret",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like({
                "Name": "int-production-use1-test-app-postgres/rds-credentials",
                "GenerateSecretString": Match.object_like({
                    "GenerateStringKey": "password",
                    "SecretStringTemplate": json.dumps({"username": "dbadmin"}, separators=(",", ":")),
                }),
            }),
        },
    )
    template.resource_count_is("AWS::SecretsManager::SecretTargetAttachment", 1)
    instance = next(iter(template.find_resources("AWS::RDS::DBInstance").values()))
    password = json.dumps(instance["Properties"]["MasterUserPassword"])
    assert "resolve:secretsmanager" in password
    outputs = json.dumps(template.to_json()["Outputs"])
    assert "resolve:secretsmanager" not in outputs
    assert "SecretString" not in outputs


def test_rds_managed_password(write_env):
    template = Template.from_stack(
        _stacks(write_env, RDS + "  password_management: rds_managed\n")
    )
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {"ManageMasterUserPassword": True, "MasterUsername": "dbadmin",
         "MasterUserPassword": Match.absent()},
    )


def test_existing_secret_reference(write_env):
    stack = _stacks(write_env, RDS + "  master_user_secret: shared-db\n", extra_secret=True)
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    instance = next(iter(template.find_resources("AWS::RDS::DBInstance").values()))
    assert "resolve:secretsmanager" in json.dumps(instance["Properties"]["MasterUserPassword"])
    assert "Fn::ImportValue" in json.dumps(instance["Properties"]["MasterUserPassword"])


def test_unknown_references_fail(write_env):
    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _stacks(write_env, RDS.replace("[db]", "[missing]"))
    with pytest.raises(ValueError, match="master_user_secret: unknown reference 'nope'"):
        _stacks(write_env, RDS + "  master_user_secret: nope\n")


def test_destroy_and_snapshot_are_explicit(write_env):
    template = Template.from_stack(
        _stacks(write_env, RDS + "  removal_policy: snapshot\n  deletion_protection: false\n")
    )
    template.has_resource("AWS::RDS::DBInstance", {"DeletionPolicy": "Snapshot"})
    template.has_resource_properties("AWS::RDS::DBInstance", {"DeletionProtection": False})
    template = Template.from_stack(_stacks(write_env, RDS + "  removal_policy: destroy\n"))
    template.has_resource("AWS::RDS::DBInstance", {"DeletionPolicy": "Delete"})
    template.has_resource("AWS::SecretsManager::Secret", {"DeletionPolicy": "Delete"})


def test_parameter_group_and_read_replica(write_env):
    text = RDS + """  parameters:
    rds.force_ssl: 1
  create_read_replica: true
  replica_instance_class: db.t3.small
  performance_insights_enabled: true
  performance_insights_retention_period: 31
  monitoring_interval: 60
"""
    template = Template.from_stack(_stacks(write_env, text))
    template.has_resource_properties(
        "AWS::RDS::DBParameterGroup",
        {
            "DBParameterGroupName": "int-production-use1-test-app-postgres-parameter-group",
            "Family": "postgres15",
            "Parameters": {"rds.force_ssl": "1"},
        },
    )
    template.resource_count_is("AWS::RDS::DBInstance", 2)
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "DBInstanceIdentifier": "int-production-use1-test-app-postgres-replica",
            "DBInstanceClass": "db.t3.small",
            "SourceDBInstanceIdentifier": Match.any_value(),
        },
    )
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "DBInstanceIdentifier": "int-production-use1-test-app-postgres",
            "EnablePerformanceInsights": True,
            "PerformanceInsightsRetentionPeriod": 31,
            "MonitoringInterval": 60,
            "MonitoringRoleArn": Match.any_value(),
        },
    )


def test_public_subnets_and_publicly_accessible(write_env):
    text = RDS + "  subnet_type: public\n  publicly_accessible: true\n"
    template = Template.from_stack(_stacks(write_env, text))
    template.has_resource_properties("AWS::RDS::DBInstance", {"PubliclyAccessible": True})
    template.has_resource_properties(
        "AWS::RDS::DBSubnetGroup",
        {"SubnetIds": Match.array_with([
            {"Fn::ImportValue": Match.string_like_regexp("PublicSubnetPrimary")},
        ])},
    )


def test_security_group_reference_crosses_stacks(write_env):
    template = Template.from_stack(_stacks(write_env))
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {"VPCSecurityGroups": [{"Fn::ImportValue": Match.string_like_regexp("SecurityGroupsDb")}]},
    )


def test_outputs_and_public_attributes(write_env):
    stack = _stacks(write_env)
    assert set(stack.instances) == {"app-postgres"}
    assert set(stack.secrets) == {"app-postgres"}
    outputs = Template.from_stack(stack).to_json()["Outputs"]
    exports = {output["Export"]["Name"] for output in outputs.values()}
    assert exports == {
        f"test-rds-app-postgres-rds-{suffix}"
        for suffix in (
            "identifier", "arn", "endpoint", "port", "subnet-group-name", "secret-arn"
        )
    }
    secret_output = next(
        output for output in outputs.values()
        if output["Export"]["Name"].endswith("secret-arn")
    )
    assert set(secret_output["Value"]) == {"Ref"}
