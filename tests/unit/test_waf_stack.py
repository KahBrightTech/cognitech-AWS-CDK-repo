"""WAFv2 IP sets, rule groups, web ACLs, associations and logging."""

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.waf import WafProps
from cognitech_cdk.stacks.waf_stack import WafStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16"
WAF = """
ip_sets:
- name: office
  addresses: [203.0.113.0/24]
rule_groups:
- name: edge
  capacity: 100
  rules:
  - {name: block-office, priority: 1, action: block, statement_type: ip_set, ip_set: office}
  - {name: sqli, priority: 2, action: block, statement_type: sqli_match, field_to_match: body,
     text_transformation: URL_DECODE}
web_acls:
- name: app
  rules:
  - {name: AWSManagedCommonRules, priority: 5, action: block, statement_type: managed_rule_group,
     vendor_name: AWS, managed_rule_group_name: AWSManagedRulesCommonRuleSet}
  - {name: geo, priority: 20, action: block, statement_type: geo_match, country_codes: [KP]}
  - {name: rate, priority: 25, action: block, statement_type: rate_based, rate_limit: 2000}
  - {name: edge, priority: 30, statement_type: rule_group_reference, rule_group: edge}
  association: {load_balancers: [app]}
  logging: {enabled: true, redacted_fields: ["single_header:authorization"]}
"""


def _stack(write_env, text=WAF):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    deps = cdk.Stack(app, "deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=2)
    alb = elbv2.ApplicationLoadBalancer(deps, "Alb", vpc=vpc)
    return WafStack(
        app, "test-waf", environment=environment, env=env,
        props=WafProps.from_dict(yaml.safe_load(text)), load_balancers={"app": alb},
    )


def test_ip_set_and_rule_group(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource_properties("AWS::WAFv2::IPSet", {
        "Name": "int-production-use1-test-office-ipset",
        "Addresses": ["203.0.113.0/24"],
        "Scope": "REGIONAL",
    })
    template.has_resource_properties("AWS::WAFv2::RuleGroup", {
        "Capacity": 100,
        "Rules": [
            Match.object_like({"Statement": {"IPSetReferenceStatement": {"Arn": {"Fn::GetAtt": Match.any_value()}}}}),
            Match.object_like({"Statement": {"SqliMatchStatement": {
                "FieldToMatch": {"Body": {}},
                "TextTransformations": [{"Priority": 0, "Type": "URL_DECODE"}],
            }}}),
        ],
    })


def test_web_acl_rules_association_and_logging(write_env):
    template = Template.from_stack(_stack(write_env))
    acl = next(iter(template.find_resources("AWS::WAFv2::WebACL").values()))["Properties"]
    assert acl["DefaultAction"] == {"Allow": {}}
    rules = {rule["Name"]: rule for rule in acl["Rules"]}
    managed = rules["AWSManagedCommonRules"]
    assert managed["OverrideAction"] == {"None": {}} and "Action" not in managed
    assert managed["Statement"]["ManagedRuleGroupStatement"]["Name"] == "AWSManagedRulesCommonRuleSet"
    assert rules["rate"]["Statement"]["RateBasedStatement"] == {"Limit": 2000, "AggregateKeyType": "IP"}
    assert "RuleGroupReferenceStatement" in rules["edge"]["Statement"]
    template.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)
    template.has_resource_properties("AWS::Logs::LogGroup", {
        "LogGroupName": "aws-waf-logs-int-production-use1-test-app-waf",
    })
    template.has_resource_properties("AWS::WAFv2::LoggingConfiguration", {
        "RedactedFields": [{"SingleHeader": {"Name": "authorization"}}],
    })
    assert {"AppWebAclArn", "OfficeIpSetArn", "EdgeRuleGroupArn"} <= set(template.find_outputs("*"))


def test_terraform_rule_file_shape_is_supported(tmp_path):
    (tmp_path / "rules.json").write_text(
        '{"description": "x", "version": "1.0", "rules": [{"name": "Common", "priority": 1, '
        '"action": "count", "statement_type": "managed_rule_group", "vendor_name": "AWS", '
        '"managed_rule_group_name": "AWSManagedRulesCommonRuleSet"}]}',
        encoding="utf-8",
    )
    props = WafProps.from_dict({"web_acls": [{"name": "a", "rule_file": "rules.json"}]}, base_dir=tmp_path)
    rule = props.web_acls[0].rules[0]
    assert rule.override_action == "count" and rule.managed_rule_group_name == "AWSManagedRulesCommonRuleSet"


@pytest.mark.parametrize("text,message", [
    ("ip_sets: [{name: a, addresses: [10.0.0.1]}]", "CIDR"),
    ("web_acls: [{name: a, rules: [{name: r, priority: 1, action: block, statement_type: ip_set, ip_set: x}]}]",
     "unknown ip_set"),
    ("rule_groups: [{name: g, capacity: 10, rules: [{name: r, priority: 1, statement_type: managed_rule_group}]}]",
     "cannot contain"),
    ("web_acls: [{name: a, rules: [{name: r, priority: 1, action: block, statement_type: geo_match}]}]",
     "country_codes"),
    ("web_acls: [{name: a, rules: [{name: r, priority: 1, statement_type: geo_match, country_codes: [US]}]}]",
     "action is required"),
    ("web_acls: [{name: a, scope: CLOUDFRONT, association: {load_balancers: [x]}}]", "only REGIONAL"),
])
def test_invalid_config_is_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        WafProps.from_dict(yaml.safe_load(text))


def test_unknown_load_balancer_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _stack(write_env, WAF.replace("load_balancers: [app]", "load_balancers: [missing]"))


def test_empty_config_builds_nothing():
    assert not WafProps.from_dict(None)
