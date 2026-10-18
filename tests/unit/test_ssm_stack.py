"""SSM parameters and documents."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.secrets import SecretsProps
from cognitech_cdk.constructs.ssm import SsmProps
from cognitech_cdk.stacks.secrets_stack import SecretsStack
from cognitech_cdk.stacks.ssm_stack import SsmStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

DOCUMENT = """
schemaVersion: "2.2"
description: Patch baseline
mainSteps:
  - action: aws:runShellScript
    name: patch
    inputs:
      runCommand: [yum -y update]
"""


def _template(write_env, raw, *, secrets=None, buckets=None, base_dir=None) -> tuple[SsmStack, Template]:
    environment = load_environment("test", root=write_env(NETWORK))
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    secret_map = None
    if secrets is not None:
        secret_map = SecretsStack(
            app, "secrets", environment=environment, props=SecretsProps.from_dict(secrets), env=env
        ).secrets
    bucket_map = None
    if buckets:
        holder = cdk.Stack(app, "buckets", env=env)
        bucket_map = {name: s3.Bucket.from_bucket_name(holder, name, bucket) for name, bucket in buckets.items()}
    stack = SsmStack(
        app, "ssm", environment=environment, props=SsmProps.from_dict(raw, base_dir=base_dir),
        secrets=secret_map, buckets=bucket_map, env=env,
    )
    return stack, Template.from_stack(stack)


# --- parsing ---------------------------------------------------------------


def test_none_and_empty_lists_are_empty():
    assert not SsmProps.from_dict(None)
    assert not SsmProps.from_dict({"parameters": [], "documents": []})


def test_secure_string_is_rejected():
    with pytest.raises(ValueError, match="SecureString cannot be created"):
        SsmProps.from_dict({"parameters": [
            {"name": "db", "description": "d", "type": "SecureString", "value": "x"},
        ]})


@pytest.mark.parametrize("fields", [{}, {"value": "x", "secret_key": "db"}])
def test_exactly_one_value_source(fields):
    with pytest.raises(ValueError, match="exactly one of value or secret_key"):
        SsmProps.from_dict({"parameters": [{"name": "p", "description": "d", **fields}]})


def test_overwrite_and_unknown_fields_are_rejected():
    with pytest.raises(ValueError, match="overwrite is not supported"):
        SsmProps.from_dict({"parameters": [{"name": "p", "description": "d", "value": "x", "overwrite": True}]})
    with pytest.raises(ValueError, match="unknown fields: extra"):
        SsmProps.from_dict({"parameters": [], "extra": []})


def test_association_fields_need_create_association():
    with pytest.raises(ValueError, match="schedule_expression require create_association"):
        SsmProps.from_dict({"documents": [
            {"name": "patch", "content": DOCUMENT, "schedule_expression": "rate(1 day)"},
        ]})


def test_document_content_must_parse():
    with pytest.raises(ValueError, match="nonempty YAML document"):
        SsmProps.from_dict({"documents": [{"name": "patch", "content": "just text"}]})


# --- parameters ------------------------------------------------------------


def test_parameters_use_repo_names_and_tiers(write_env):
    stack, template = _template(write_env, {"parameters": [
        {"name": "app-url", "description": "App URL", "type": "String", "value": "https://app"},
        {"name": "zones", "description": "AZs", "type": "StringList", "value": "a,b", "tier": "Advanced"},
        {"name": "path", "description": "Path param", "value": "x", "parameter_name": "/app/config/path"},
    ]})
    template.has_resource_properties("AWS::SSM::Parameter", {
        "Name": "int-production-use1-uat-app-url", "Type": "String", "Value": "https://app",
        "Description": "App URL", "Tier": "Standard",
        "Tags": {"Name": "int-production-use1-uat-app-url"},
    })
    template.has_resource_properties("AWS::SSM::Parameter", {
        "Name": "int-production-use1-uat-zones", "Type": "StringList", "Value": "a,b", "Tier": "Advanced",
    })
    template.has_resource_properties("AWS::SSM::Parameter", {"Name": "/app/config/path"})
    assert set(stack.parameters) == {"app-url", "zones", "path"}
    outputs = template.find_outputs("*")
    assert outputs["AppUrlParameterName"]["Export"]["Name"] == "ssm-app-url-parameter-name"
    assert outputs["AppUrlParameterArn"]["Export"]["Name"] == "ssm-app-url-parameter-arn"


def test_secret_key_stores_the_secret_name_not_its_value(write_env):
    _, template = _template(
        write_env,
        {"parameters": [{"name": "db-secret", "description": "DB secret name", "secret_key": "db"}]},
        secrets=[{"key": "db", "name": "db", "description": "DB credentials"}],
    )
    parameter = next(iter(template.find_resources("AWS::SSM::Parameter").values()))["Properties"]
    assert parameter["Type"] == "String"
    value = json.dumps(parameter["Value"])
    assert "Fn::ImportValue" in value or "int-production-use1-uat-db" in value
    assert "GetSecretValue" not in value and "resolve:secretsmanager" not in value


def test_unknown_secret_key_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _template(
            write_env,
            {"parameters": [{"name": "p", "description": "d", "secret_key": "missing"}]},
            secrets=[],
        )


# --- documents -------------------------------------------------------------


def test_document_from_file_with_association(write_env, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "patch.yaml").write_text(DOCUMENT, encoding="utf-8")
    stack, template = _template(
        write_env,
        {"documents": [{
            "name": "patch",
            "content_file": "docs/patch.yaml",
            "tags": {"Team": "ops"},
            "create_association": True,
            "targets": {"key": "tag:Patch", "values": ["yes"]},
            "schedule_expression": "rate(1 day)",
            "output_location": {"bucket": "logs", "s3_key_prefix": "ssm/"},
        }]},
        buckets={"logs": "my-log-bucket"},
        base_dir=tmp_path,
    )
    template.has_resource_properties("AWS::SSM::Document", {
        "Name": "int-production-use1-uat-patch",
        "DocumentType": "Command",
        "DocumentFormat": "YAML",
        "UpdateMethod": "NewVersion",
        "Content": {"schemaVersion": "2.2", "mainSteps": Match.any_value(), "description": "Patch baseline"},
        "Tags": Match.array_with([{"Key": "Name", "Value": "int-production-use1-uat-patch"}]),
    })
    template.has_resource_properties("AWS::SSM::Document", {
        "Tags": Match.array_with([{"Key": "Team", "Value": "ops"}]),
    })
    template.has_resource_properties("AWS::SSM::Association", {
        "Name": {"Ref": Match.string_like_regexp("patch")},
        "Targets": [{"Key": "tag:Patch", "Values": ["yes"]}],
        "ScheduleExpression": "rate(1 day)",
        "OutputLocation": {"S3Location": {"OutputS3BucketName": "my-log-bucket", "OutputS3KeyPrefix": "ssm/"}},
    })
    assert set(stack.documents) == {"patch"} and set(stack.associations) == {"patch"}
    outputs = template.find_outputs("*")
    assert outputs["PatchDocumentName"]["Export"]["Name"] == "ssm-patch-document-name"
    assert outputs["PatchAssociationId"]["Export"]["Name"] == "ssm-patch-association-id"


def test_document_without_association(write_env):
    stack, template = _template(write_env, {"documents": [
        {"name": "run", "content": DOCUMENT, "document_format": "JSON", "document_type": "Command"},
    ]})
    template.resource_count_is("AWS::SSM::Document", 1)
    template.resource_count_is("AWS::SSM::Association", 0)
    template.has_resource_properties("AWS::SSM::Document", {"DocumentFormat": "JSON"})
    assert stack.associations == {}


def test_unknown_output_bucket_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'logs'"):
        _template(write_env, {"documents": [{
            "name": "patch", "content": DOCUMENT, "create_association": True,
            "output_location": {"bucket": "logs"},
        }]})
