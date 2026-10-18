"""Secrets Manager secrets: generated values only, safe resource policies."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.secrets import SecretsProps
from cognitech_cdk.stacks.secrets_stack import SecretsStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

OPENSEARCH = [{
    "key": "opensearch",
    "name_prefix": "opensearch-master",
    "description": "OpenSearch master user credentials.",
    "recovery_window_in_days": 7,
    "generate": {"template": {"username": "admin"}, "key": "password", "length": 32,
                 "exclude_characters": "\"@/\\"},
}]


def _stack(write_env, raw, *, base_dir=None) -> tuple[SecretsStack, Template]:
    environment = load_environment("test", root=write_env(NETWORK))
    app = cdk.App()
    stack = SecretsStack(
        app, "secrets", environment=environment,
        props=SecretsProps.from_dict(raw, base_dir=base_dir),
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    return stack, Template.from_stack(stack)


# --- parsing ---------------------------------------------------------------


def test_none_is_empty():
    props = SecretsProps.from_dict(None)
    assert not props and props.secrets == ()


def test_literal_values_are_rejected():
    with pytest.raises(ValueError, match="literal 'value' is not supported"):
        SecretsProps.from_dict([{
            "key": "db", "name": "db", "description": "d",
            "value": {"username": "u", "password": "hunter2"},
        }])


@pytest.mark.parametrize("names", [{}, {"name": "a", "name_prefix": "b"}])
def test_exactly_one_of_name_or_prefix(names):
    with pytest.raises(ValueError, match="exactly one of name or name_prefix"):
        SecretsProps.from_dict([{"key": "db", "description": "d", **names}])


def test_template_fields_must_not_look_secret():
    with pytest.raises(ValueError, match="looks secret"):
        SecretsProps.from_dict([{
            "key": "db", "name": "db", "description": "d",
            "generate": {"template": {"password": "x"}, "key": "other"},
        }])


def test_template_and_key_go_together():
    with pytest.raises(ValueError, match="template and key must be set together"):
        SecretsProps.from_dict([{
            "key": "db", "name": "db", "description": "d", "generate": {"key": "password"},
        }])


def test_unknown_fields_and_duplicates_are_rejected():
    with pytest.raises(ValueError, match="unknown fields: kms"):
        SecretsProps.from_dict([{"key": "db", "name": "db", "description": "d", "kms": "x"}])
    with pytest.raises(ValueError, match="duplicate name 'DB'"):
        SecretsProps.from_dict([
            {"key": "db", "name": "a", "description": "d"},
            {"key": "DB", "name": "b", "description": "d"},
        ])


def test_recovery_window_is_validated():
    with pytest.raises(ValueError, match="0 or between 7 and 30"):
        SecretsProps.from_dict([{"key": "db", "name": "db", "description": "d", "recovery_window_in_days": 3}])


def test_terraform_public_policy_is_rejected(tmp_path):
    (tmp_path / "policy.json").write_text(json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowGetAndPutSecret", "Effect": "Allow", "Principal": {"AWS": "*"},
            "Action": ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
            "Resource": "*",
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="Principal '\\*' is public"):
        SecretsProps.from_dict(
            [{"key": "db", "name": "db", "description": "d", "policy": "policy.json"}], base_dir=tmp_path
        )


def test_wildcard_principal_with_account_condition_is_allowed():
    props = SecretsProps.from_dict([{
        "key": "db", "name": "db", "description": "d",
        "policy": {"Statement": [{
            "Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "secretsmanager:GetSecretValue",
            "Resource": "*", "Condition": {"StringEquals": {"aws:PrincipalAccount": "[[account]]"}},
        }]},
    }])
    assert props.secrets[0].policy is not None


# --- synthesized resources -------------------------------------------------


def test_secret_value_is_generated_by_secrets_manager(write_env):
    stack, template = _stack(write_env, OPENSEARCH)

    template.resource_count_is("AWS::SecretsManager::Secret", 1)
    template.has_resource("AWS::SecretsManager::Secret", {
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": {
            "Name": "int-production-use1-uat-opensearch-master",
            "Description": "OpenSearch master user credentials.",
            "GenerateSecretString": {
                "SecretStringTemplate": '{"username": "admin"}',
                "GenerateStringKey": "password",
                "PasswordLength": 32,
                "ExcludeCharacters": "\"@/\\",
            },
            "Tags": Match.array_with([{"Key": "Name", "Value": "int-production-use1-uat-opensearch-master"}]),
        },
    })
    resource = next(iter(template.find_resources("AWS::SecretsManager::Secret").values()))
    assert "SecretString" not in resource["Properties"]
    template.resource_count_is("AWS::SecretsManager::ResourcePolicy", 0)
    assert set(stack.secrets) == {"opensearch"}


def test_destroy_is_explicit(write_env):
    _, template = _stack(write_env, [{**OPENSEARCH[0], "removal_policy": "destroy"}])
    template.has_resource("AWS::SecretsManager::Secret", {"DeletionPolicy": "Delete"})


def test_policy_placeholders_render_to_pseudo_parameters(write_env):
    _, template = _stack(write_env, [{
        **OPENSEARCH[0],
        "policy": {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::[[account]]:root"},
            "Action": "secretsmanager:GetSecretValue", "Resource": "*",
        }]},
    }])
    template.has_resource_properties("AWS::SecretsManager::ResourcePolicy", {
        "ResourcePolicy": {"Statement": [Match.object_like({
            "Principal": {"AWS": {"Fn::Join": ["", ["arn:aws:iam::", {"Ref": "AWS::AccountId"}, ":root"]]}},
        })]},
    })


def test_outputs_export_arn_and_name(write_env):
    _, template = _stack(write_env, OPENSEARCH)
    outputs = template.find_outputs("*")
    assert outputs["OpensearchSecretArn"]["Export"]["Name"] == "secrets-opensearch-secret-arn"
    assert outputs["OpensearchSecretName"]["Export"]["Name"] == "secrets-opensearch-secret-name"
