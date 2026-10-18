"""Firehose delivery streams to S3 and OpenSearch."""

import json

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_iam as iam
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.firehose import FirehoseProps
from cognitech_cdk.stacks.firehose_stack import FirehoseStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16"
S3_STREAM = """
- name: audit
  s3_configuration: {bucket: audit, prefix: firehose/}
"""
OPENSEARCH_STREAM = """
- name: logs
  destination: opensearch
  role: firehose
  s3_configuration: {bucket: audit}
  opensearch_configuration: {domain: logs, index_name: eks, index_rotation_period: OneWeek}
"""


def _stack(write_env, text):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    deps = cdk.Stack(app, "deps", env=env)
    return FirehoseStack(
        app, "test-firehose", environment=environment, env=env,
        props=FirehoseProps.from_dict(yaml.safe_load(text)),
        buckets={"audit": s3.Bucket(deps, "Audit")},
        domains={"logs": opensearch.Domain(
            deps, "Logs", version=opensearch.EngineVersion.OPENSEARCH_2_11
        )},
        roles={"firehose": iam.Role(
            deps, "Role", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com")
        )},
    )


def test_s3_stream_creates_scoped_role_and_logging(write_env):
    template = Template.from_stack(_stack(write_env, S3_STREAM))
    template.has_resource_properties("AWS::KinesisFirehose::DeliveryStream", {
        "DeliveryStreamName": "int-production-use1-test-audit",
        "DeliveryStreamType": "DirectPut",
        "ExtendedS3DestinationConfiguration": Match.object_like({
            "Prefix": "firehose/",
            "ErrorOutputPrefix": "errors/",
            "CompressionFormat": "GZIP",
            "BufferingHints": {"SizeInMBs": 5, "IntervalInSeconds": 300},
            "CloudWatchLoggingOptions": Match.object_like({"Enabled": True}),
        }),
    })
    template.has_resource_properties("AWS::Logs::LogGroup", {
        "LogGroupName": "/aws/kinesisfirehose/int-production-use1-test-audit",
        "RetentionInDays": 14,
    })
    policy = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "s3:PutObject" in policy and '"Resource": "*"' not in policy
    assert {"AuditStreamName", "AuditStreamArn"} <= set(template.find_outputs("*"))


def test_opensearch_stream_uses_existing_role_and_s3_backup(write_env):
    template = Template.from_stack(_stack(write_env, OPENSEARCH_STREAM))
    template.resource_count_is("AWS::IAM::Role", 0)
    template.has_resource_properties("AWS::KinesisFirehose::DeliveryStream", {
        "AmazonopensearchserviceDestinationConfiguration": Match.object_like({
            "IndexName": "eks",
            "IndexRotationPeriod": "OneWeek",
            "S3BackupMode": "FailedDocumentsOnly",
            "DomainARN": {"Fn::ImportValue": Match.any_value()},
            "S3Configuration": Match.object_like({"BucketARN": {"Fn::ImportValue": Match.any_value()}}),
        }),
    })


@pytest.mark.parametrize("text,message", [
    ("- name: a\n", "s3_configuration is required"),
    ("- name: a\n  destination: opensearch\n  s3_configuration: {bucket: b}\n", "needs opensearch_configuration"),
    ("- name: a\n  s3_configuration: {bucket: b, bucket_arn: arn:aws:s3:::b}\n", "exactly one of bucket"),
    ("- name: a\n  s3_configuration: {bucket: b}\n  server_side_encryption: {key_type: CUSTOMER_MANAGED_CMK}\n",
     "key_arn"),
    ("- name: a\n  s3_configuration: {bucket: b}\n  cloudwatch_log_retention_days: 2\n", "retention"),
])
def test_invalid_config_is_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        FirehoseProps.from_dict(yaml.safe_load(text))


def test_disabled_streams_are_skipped():
    assert not FirehoseProps.from_dict(yaml.safe_load("- name: a\n  create: false\n"))


def test_unknown_bucket_reference_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _stack(write_env, S3_STREAM.replace("bucket: audit", "bucket: missing"))
