"""EC2 key pairs: private keys stay inside AWS and are never public."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.key_pairs import KeyPairsProps
from cognitech_cdk.stacks.key_pairs_stack import KeyPairsStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""


def _stack(write_env, raw, *, base_dir=None) -> tuple[KeyPairsStack, Template]:
    environment = load_environment("test", root=write_env(NETWORK))
    app = cdk.App()
    stack = KeyPairsStack(
        app, "keys", environment=environment,
        props=KeyPairsProps.from_dict(raw, base_dir=base_dir),
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    return stack, Template.from_stack(stack)


# --- parsing ---------------------------------------------------------------


def test_none_is_empty():
    assert not KeyPairsProps.from_dict(None)


def test_name_prefix_and_orphan_secret_fields_are_rejected():
    with pytest.raises(ValueError, match="name_prefix is not supported"):
        KeyPairsProps.from_dict([{"name": "bastion", "name_prefix": "b", "create_secret": False}])
    with pytest.raises(ValueError, match="secret_name require create_secret: true"):
        KeyPairsProps.from_dict([{"name": "bastion", "secret_name": "x"}])
    with pytest.raises(ValueError, match="key_type must be one of"):
        KeyPairsProps.from_dict([{"name": "bastion", "key_type": "dsa"}])


def test_terraform_wildcard_secret_policy_is_rejected():
    with pytest.raises(ValueError, match="is public"):
        KeyPairsProps.from_dict([{
            "name": "bastion", "create_secret": True,
            "policy": {"Statement": [{
                "Effect": "Allow", "Principal": {"AWS": "*"},
                "Action": ["secretsmanager:GetSecretValue"], "Resource": "*",
            }]},
        }])


# --- synthesized resources -------------------------------------------------


def test_ec2_generates_the_key_without_material_in_the_template(write_env):
    stack, template = _stack(write_env, [{"name": "bastion", "create_secret": False}])

    template.has_resource("AWS::EC2::KeyPair", {
        "DeletionPolicy": "Retain",
        "Properties": {
            "KeyName": "int-production-use1-uat-bastion-key",
            "KeyType": "rsa",
            "KeyFormat": "pem",
            "Tags": Match.array_with([{"Key": "Name", "Value": "int-production-use1-uat-bastion-key"}]),
        },
    })
    resource = next(iter(template.find_resources("AWS::EC2::KeyPair").values()))
    assert "PublicKeyMaterial" not in resource["Properties"]
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    template.resource_count_is("AWS::Lambda::Function", 0)
    assert set(stack.key_pairs) == {"bastion"} and stack.secrets == {}

    outputs = template.find_outputs("*")
    assert outputs["BastionKeyPairName"]["Export"]["Name"] == "keys-bastion-key-pair-name"
    assert outputs["BastionKeyPairId"]["Export"]["Name"] == "keys-bastion-key-pair-id"
    assert "/ec2/keypair/" in json.dumps(outputs["BastionPrivateKeyParameterName"]["Value"])


def test_private_key_is_copied_inside_aws_with_account_only_access(write_env):
    stack, template = _stack(write_env, [{
        "name": "bastion", "create_secret": True, "secret_name": "bastion-pem",
        "secret_description": "Bastion private key",
    }])

    template.has_resource("AWS::SecretsManager::Secret", {
        "DeletionPolicy": "Retain",
        "Properties": {"Name": "int-production-use1-uat-bastion-pem", "Description": "Bastion private key"},
    })
    template.has_resource_properties("AWS::SecretsManager::ResourcePolicy", {
        "ResourcePolicy": {"Statement": [{
            "Sid": "AccountRootOnly",
            "Effect": "Allow",
            "Principal": {"AWS": {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::111122223333:root"]]}},
            "Action": ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
            "Resource": "*",
        }]},
    })
    template.has_resource_properties("Custom::KeyPairPrivateKeyToSecret", {
        "ParameterName": {"Fn::Join": ["", ["/ec2/keypair/", Match.any_value()]]},
        "SecretArn": {"Ref": Match.any_value()},
    })
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {"Statement": Match.array_with([
            Match.object_like({"Action": "ssm:GetParameter", "Resource": {
                "Fn::Join": ["", Match.array_with([Match.string_like_regexp("parameter/ec2/keypair/")])],
            }}),
            Match.object_like({"Action": "secretsmanager:PutSecretValue", "Resource": {"Ref": Match.any_value()}}),
        ])},
    })
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {"Statement": Match.array_with([
            Match.object_like({"Action": "kms:Decrypt", "Condition": {
                "StringEquals": {"kms:ViaService": "ssm.us-east-1.amazonaws.com"},
            }}),
        ])},
    })
    assert set(stack.secrets) == {"bastion"}
    outputs = template.find_outputs("*")
    assert outputs["BastionPrivateKeySecretArn"]["Export"]["Name"] == "keys-bastion-private-key-secret-arn"

    rendered = json.dumps(template.to_json())
    assert "PRIVATE KEY" not in rendered
    for policy in template.find_resources("AWS::SecretsManager::ResourcePolicy").values():
        for statement in policy["Properties"]["ResourcePolicy"]["Statement"]:
            assert statement["Principal"] != "*" and statement["Principal"].get("AWS") != "*"


def test_copy_function_never_returns_or_logs_the_key(write_env):
    _, template = _stack(write_env, [{"name": "a", "create_secret": True}, {"name": "b", "create_secret": True}])
    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 1
    code = next(iter(functions.values()))["Properties"]["Code"]["ZipFile"]
    assert '"Data": {}' in code and '"NoEcho": True' in code
    assert "print(" not in code and "logging" not in code and "str(error)" not in code
    template.resource_count_is("Custom::KeyPairPrivateKeyToSecret", 2)
