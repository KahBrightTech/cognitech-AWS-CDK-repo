"""WAFv2 IP sets, rule groups, web ACLs, ALB associations and logging."""

from __future__ import annotations

from typing import Any, Mapping

import aws_cdk as cdk
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_logs as logs
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.ecr import logical_id
from cognitech_cdk.constructs.waf import RuleProps, WafProps, WebAclProps, statement
from cognitech_cdk.stacks.base import EnvironmentStack


def _metric(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)[:128]


class WafStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: WafProps,
        load_balancers: Mapping[str, elbv2.BaseLoadBalancer] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        self._load_balancers = load_balancers or {}
        self.ip_sets: dict[str, wafv2.CfnIPSet] = {}
        self.rule_groups: dict[str, wafv2.CfnRuleGroup] = {}
        self.web_acls: dict[str, wafv2.CfnWebACL] = {}

        for ip_set in props.ip_sets:
            name = self.resource_name(ip_set.name, "ipset")
            resource = wafv2.CfnIPSet(
                self, f"{logical_id(ip_set.name)}IpSet",
                name=name,
                description=ip_set.description or f"IP Set for WAF - {ip_set.name}",
                scope="REGIONAL",
                ip_address_version=ip_set.ip_address_version,
                addresses=list(ip_set.addresses),
            )
            self._tag(resource, name, ip_set.tags)
            self.ip_sets[ip_set.name] = resource
            self.output(f"{logical_id(ip_set.name)}IpSetArn", resource.attr_arn, f"{ip_set.name}-ipset-arn")

        for group in props.rule_groups:
            name = self.resource_name(group.name, "rulegroup")
            resource = wafv2.CfnRuleGroup(
                self, f"{logical_id(group.name)}RuleGroup",
                name=name,
                description=group.description or f"WAF Rule Group - {group.name}",
                scope="REGIONAL",
                capacity=group.capacity,
                visibility_config=self._visibility(wafv2.CfnRuleGroup.VisibilityConfigProperty, name, True, True),
            )
            resource.add_property_override("Rules", [self._rule(rule, True, True) for rule in group.rules])
            self._tag(resource, name, group.tags)
            self.rule_groups[group.name] = resource
            self.output(
                f"{logical_id(group.name)}RuleGroupArn", resource.attr_arn, f"{group.name}-rulegroup-arn"
            )

        for acl in props.web_acls:
            self._web_acl(acl)

    @staticmethod
    def _tag(resource: cdk.CfnResource, name: str, tags: Mapping[str, str] = {}) -> None:
        cdk.Tags.of(resource).add("Name", name)
        for key, value in tags.items():
            cdk.Tags.of(resource).add(key, value)

    @staticmethod
    def _visibility(struct: type, name: str, metrics: bool, sampled: bool) -> Any:
        return struct(
            cloud_watch_metrics_enabled=metrics,
            metric_name=_metric(name),
            sampled_requests_enabled=sampled,
        )

    def _rule(self, rule: RuleProps, metrics: bool, sampled: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "Name": rule.name,
            "Priority": rule.priority,
            "Statement": statement(
                rule,
                ip_set_arn=self.ip_sets[rule.ip_set].attr_arn if rule.ip_set else rule.ip_set_arn,
                rule_group_arn=(
                    self.rule_groups[rule.rule_group].attr_arn if rule.rule_group else rule.rule_group_arn
                ),
            ),
            "VisibilityConfig": {
                "CloudWatchMetricsEnabled": metrics,
                "MetricName": _metric(rule.name),
                "SampledRequestsEnabled": sampled,
            },
        }
        if rule.override_action is not None:
            result["OverrideAction"] = {rule.override_action.capitalize(): {}}
        else:
            result["Action"] = {(rule.action or "").capitalize(): {}}
        return result

    def _web_acl(self, acl: WebAclProps) -> None:
        label = f"WAF web ACL '{acl.name}'"
        cid = logical_id(acl.name)
        name = self.resource_name(acl.name, "waf")
        resource = wafv2.CfnWebACL(
            self, f"{cid}WebAcl",
            name=name,
            description=acl.description,
            scope=acl.scope,
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow={} if acl.default_action == "allow" else None,
                block={} if acl.default_action == "block" else None,
            ),
            visibility_config=self._visibility(
                wafv2.CfnWebACL.VisibilityConfigProperty, f"{name}-metric",
                acl.metrics_enabled, acl.sampled_requests_enabled,
            ),
        )
        resource.add_property_override(
            "Rules", [self._rule(rule, acl.metrics_enabled, acl.sampled_requests_enabled) for rule in acl.rules]
        )
        self._tag(resource, name)
        self.web_acls[acl.name] = resource
        self.output(f"{cid}WebAclArn", resource.attr_arn, f"{acl.name}-waf-arn")
        self.output(f"{cid}WebAclId", resource.attr_id, f"{acl.name}-waf-id")

        targets = [
            (key, schema.lookup(self._load_balancers, key, f"{label}: association").load_balancer_arn)
            for key in acl.load_balancers
        ] + [(f"Arn{index}", arn) for index, arn in enumerate(acl.load_balancer_arns)]
        for key, arn in targets:
            wafv2.CfnWebACLAssociation(
                self, f"{cid}{logical_id(key)}Association",
                resource_arn=arn, web_acl_arn=resource.attr_arn,
            )

        if acl.logging_enabled:
            # WAF only logs to groups named aws-waf-logs-*.
            log_name = f"aws-waf-logs-{name}"
            group = logs.CfnLogGroup(
                self, f"{cid}LogGroup", log_group_name=log_name, retention_in_days=acl.log_retention_days
            )
            group.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
            redacted = []
            for item in acl.redacted_fields:
                if item.startswith("single_header:"):
                    redacted.append({"SingleHeader": {"Name": item.split(":", 1)[1]}})
                else:
                    redacted.append({
                        "uri_path": {"UriPath": {}}, "query_string": {"QueryString": {}}, "method": {"Method": {}},
                    }[item])
            logging = wafv2.CfnLoggingConfiguration(
                self, f"{cid}Logging",
                resource_arn=resource.attr_arn,
                log_destination_configs=[
                    self.format_arn(service="logs", resource="log-group", resource_name=log_name,
                                    arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)
                ],
            )
            if redacted:
                logging.add_property_override("RedactedFields", redacted)
            logging.add_resource_dependency(group)
