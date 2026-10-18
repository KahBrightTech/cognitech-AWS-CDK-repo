"""EventBridge buses, rules, targets and Lambda invoke permissions."""

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.events import EventsProps
from cognitech_cdk.stacks.events_stack import EventsStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16"
EVENTS = """
- name: node-tagger
  description: Tag EKS nodes
  event_pattern:
    source: [aws.ec2]
    detail-type: [EC2 Instance State-change Notification]
  target: tagger
  tags: {Used_for: tagging}
"""


def _stack(write_env, text=EVENTS, *, function=True):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    functions = {}
    if function:
        source = cdk.Stack(app, "functions", env=env)
        functions["tagger"] = lambda_.Function(
            source, "Tagger", runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler", code=lambda_.Code.from_inline("def handler(*_): pass"),
        )
    return EventsStack(
        app, "test-events", environment=environment, env=env,
        props=EventsProps.from_dict(yaml.safe_load(text)), functions=functions,
    )


def test_lambda_target_gets_rule_scoped_permission(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource_properties("AWS::Events::Rule", {
        "Name": "int-production-use1-test-node-tagger",
        "State": "ENABLED",
        "EventPattern": {"source": ["aws.ec2"]},
        "Targets": [{"Id": "Target0", "Arn": {"Fn::ImportValue": Match.any_value()}}],
        "Tags": Match.array_with([{"Key": "Used_for", "Value": "tagging"}]),
    })
    template.has_resource_properties("AWS::Lambda::Permission", {
        "Action": "lambda:InvokeFunction",
        "Principal": "events.amazonaws.com",
        "SourceArn": {"Fn::GetAtt": [Match.any_value(), "Arn"]},
    })
    assert set(template.find_outputs("*")) == {"NodeTaggerRuleArn", "NodeTaggerRuleName"}


def test_custom_bus_and_target_arn(write_env):
    text = """
- name: audit
  event_pattern: '{"source": ["aws.s3"]}'
  target_arn: arn:aws:sqs:us-east-1:111122223333:audit
  event_bus: audit-bus
  enabled: false
"""
    template = Template.from_stack(_stack(write_env, text, function=False))
    template.has_resource_properties(
        "AWS::Events::EventBus", {"Name": "int-production-use1-test-audit-bus"}
    )
    template.has_resource_properties("AWS::Events::Rule", {
        "State": "DISABLED",
        "EventBusName": {"Ref": Match.any_value()},
        "Targets": [{"Id": "Target0", "Arn": "arn:aws:sqs:us-east-1:111122223333:audit"}],
    })
    template.resource_count_is("AWS::Lambda::Permission", 0)


@pytest.mark.parametrize("text,message", [
    ("- name: x\n  event_pattern: {source: [a]}\n", "exactly one of target or target_arn"),
    ("- name: x\n  event_pattern: {}\n  target: f\n", "nonempty mapping"),
    ("- name: x\n  event_pattern: '{bad'\n  target: f\n", "invalid JSON"),
    ("- name: x\n  event_pattern: {source: [a]}\n  target_arn: nope\n", "must be an ARN"),
    ("- name: x\n  event_pattern: {source: [a]}\n  target: f\n  extra: 1\n", "unknown fields"),
])
def test_invalid_config_is_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        EventsProps.from_dict(yaml.safe_load(text))


def test_unknown_function_reference_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'tagger'"):
        _stack(write_env, function=False)


def test_empty_config_builds_nothing():
    assert not EventsProps.from_dict(None)
