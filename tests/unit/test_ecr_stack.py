"""ECR repositories: names, encryption, scanning, policies and outputs."""

import json

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.ecr import EcrProps
from cognitech_cdk.stacks.ecr_stack import EcrStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16"
ECR = """
- name: backend
  image_tag_mutability: IMMUTABLE
  lifecycle_policy:
    rules:
    - rulePriority: 1
      selection: {tagStatus: untagged, countType: sinceImagePushed, countUnit: days, countNumber: 14}
      action: {type: expire}
  repository_policy:
    Version: "2012-10-17"
    Statement:
    - Effect: Allow
      Principal: {AWS: "arn:aws:iam::[[account_number]]:root"}
      Action: [ecr:BatchGetImage]
"""


def _stack(write_env, text=ECR):
    environment = load_environment("test", root=write_env(NETWORK))
    return EcrStack(
        cdk.App(), "test-ecr", environment=environment,
        env=cdk.Environment(account=environment.account_id, region=environment.region),
        props=EcrProps.from_dict(yaml.safe_load(text)),
    )


def test_repository_defaults_and_policies(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource("AWS::ECR::Repository", {
        "DeletionPolicy": "Retain",
        "Properties": Match.object_like({
            "RepositoryName": "int-production-use1-test-backend",
            "ImageTagMutability": "IMMUTABLE",
            "ImageScanningConfiguration": {"ScanOnPush": True},
            "EncryptionConfiguration": {"EncryptionType": "AES256"},
        }),
    })
    repository = next(iter(template.find_resources("AWS::ECR::Repository").values()))
    lifecycle = json.loads(repository["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"])
    assert lifecycle["rules"][0]["action"] == {"type": "expire"}
    policy = json.dumps(repository["Properties"]["RepositoryPolicyText"])
    assert "AWS::AccountId" in policy and "[[" not in policy
    assert {"BackendRepositoryArn", "BackendRepositoryUri", "BackendRepositoryName"} <= set(
        template.find_outputs("*")
    )


@pytest.mark.parametrize("text,message", [
    ("- name: Bad_Name\n", "name"),
    ("- name: a\n  encryption_type: KMS\n", "kms_key_arn"),
    ("- name: a\n  image_tag_mutability: SOMETIMES\n", "image_tag_mutability"),
    ("- name: a\n  extra: 1\n", "unknown fields"),
])
def test_invalid_config_is_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        EcrProps.from_dict(yaml.safe_load(text))


def test_empty_config_builds_nothing():
    assert not EcrProps.from_dict(None)
