"""ECS clusters, capacity, task definitions and services."""

from __future__ import annotations

from typing import Mapping

from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.ecr import logical_id
from cognitech_cdk.constructs.ecs import EcsClusters, EcsProps
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.stacks.base import EnvironmentStack


class EcsStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: EcsProps,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        roles: Mapping[str, iam.IRole] | None = None,
        instance_profiles: Mapping[str, iam.CfnInstanceProfile] | None = None,
        target_groups: Mapping[str, elbv2.ITargetGroup] | None = None,
        load_balancers: Mapping[str, elbv2.BaseLoadBalancer] | None = None,
        repositories: Mapping[str, ecr.IRepository] | None = None,
        key_pairs: Mapping[str, ec2.IKeyPair] | None = None,
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        self.ecs = EcsClusters(
            self,
            "Ecs",
            props=props,
            common=environment.common,
            name=self.resource_name,
            network=network,
            security_groups=security_groups,
            roles=roles,
            target_groups=target_groups,
            repositories=repositories,
            key_pairs=key_pairs,
            secrets=secrets,
            load_balancers=load_balancers,
            instance_profiles=instance_profiles,
        )
        self.clusters = self.ecs.clusters
        self.services = self.ecs.services
        self.task_definitions = self.ecs.task_definitions
        self.capacity_providers = self.ecs.capacity_providers

        for name, cluster in self.ecs.cluster_resources.items():
            base = logical_id(name)
            self.output(f"{base}ClusterName", cluster.ref, f"{name}-cluster-name")
            self.output(f"{base}ClusterArn", cluster.attr_arn, f"{name}-cluster-arn")
        for name, service in self.ecs.services.items():
            base = logical_id(name)
            self.output(f"{base}ServiceName", service.attr_name, f"{name}-service-name")
            self.output(f"{base}ServiceArn", service.attr_service_arn, f"{name}-service-arn")
        for name, task in self.ecs.task_definitions.items():
            self.output(
                f"{logical_id(name)}TaskDefinitionArn", task.ref, f"{name}-task-definition-arn"
            )
