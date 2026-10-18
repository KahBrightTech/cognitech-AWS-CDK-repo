"""Load balancers, target groups, listeners and listener rules."""

from __future__ import annotations

from typing import Mapping

from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_s3 as s3
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.load_balancing import (
    LoadBalancing,
    LoadBalancingProps,
    construct_id,
)
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.stacks.base import EnvironmentStack


class LoadBalancingStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        environment: Environment,
        props: LoadBalancingProps,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        certificates: Mapping[str, acm.ICertificate] | None = None,
        buckets: Mapping[str, s3.IBucket] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id_, environment=environment, **kwargs)

        self.load_balancing = LoadBalancing(
            self,
            "LoadBalancing",
            common=environment.common,
            network_name=environment.network.name,
            account_id=environment.account_id,
            props=props,
            network=network,
            security_groups=security_groups,
            certificates=certificates,
            buckets=buckets,
        )
        self.load_balancers: dict[str, elbv2.BaseLoadBalancer] = self.load_balancing.load_balancers
        self.target_groups: dict[str, elbv2.TargetGroupBase] = self.load_balancing.target_groups
        self.listeners: dict[str, elbv2.CfnListener] = self.load_balancing.listeners
        self.listener_rules: dict[str, elbv2.CfnListenerRule] = self.load_balancing.listener_rules

        for name, lb in self.load_balancers.items():
            cid = construct_id(name)
            self.output(f"{cid}LoadBalancerArn", lb.load_balancer_arn, f"{name}-lb-arn")
            self.output(f"{cid}LoadBalancerName", lb.load_balancer_name, f"{name}-lb-name")
            self.output(f"{cid}LoadBalancerDnsName", lb.load_balancer_dns_name, f"{name}-lb-dns-name")
            self.output(
                f"{cid}LoadBalancerHostedZoneId",
                lb.load_balancer_canonical_hosted_zone_id,
                f"{name}-lb-hosted-zone-id",
            )
        for name, group in self.target_groups.items():
            cid = construct_id(name)
            self.output(f"{cid}TargetGroupArn", group.target_group_arn, f"{name}-tg-arn")
            self.output(f"{cid}TargetGroupName", group.target_group_name, f"{name}-tg-name")
        for name, listener in self.listeners.items():
            self.output(
                f"{construct_id(name)}ListenerArn", listener.attr_listener_arn, f"{name}-listener-arn"
            )
        for name, rule in self.listener_rules.items():
            self.output(
                f"{construct_id(name)}ListenerRuleArn", rule.attr_rule_arn, f"{name}-listener-rule-arn"
            )
