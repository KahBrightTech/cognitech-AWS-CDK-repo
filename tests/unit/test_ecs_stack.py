"""ECS clusters, task definitions, services and EC2 capacity."""

import json

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.ecs import EcsProps
from cognitech_cdk.stacks.ecs_stack import EcsStack
from cognitech_cdk.stacks.network_stack import NetworkStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16\nazs: 2"
FARGATE = """
- name: app
  container_insights: true
  capacity_providers: {names: [FARGATE]}
  task_definitions:
  - family: backend
    task_role: task
    execution_role: execution
    network_mode: awsvpc
    requires_compatibilities: [FARGATE]
    cpu: 512
    memory: 1024
    database_secret: db
    container_definitions:
    - name: backend
      repository: backend
      image_tag: v1
      port_mappings: [{container_port: 3000}]
      environment: {NODE_ENV: production}
      secrets: [{name: JWT, secret: db, key: jwt}]
      log_configuration: {log_driver: awslogs}
  services:
  - name: backend
    task_definition: backend
    desired_count: 2
    capacity_provider_strategy: [{capacity_provider: FARGATE, weight: 1}]
    load_balancers: [{target_group: backend, container_name: backend, container_port: 3000}]
    network_configuration: {subnets: public, security_groups: [app], assign_public_ip: true}
"""
EC2 = """
- name: workers
  capacity_providers:
    names: [workers-cp]
    default_strategy: [{capacity_provider: workers-cp, weight: 1}]
  ec2_autoscaling:
    launch_templates:
    - {name: workers, instance_type: t3.medium, instance_profile: ecs, security_groups: [app], volume_size: 30}
    autoscaling_group: {name: workers, min_size: 1, max_size: 2, subnets: public}
    capacity_provider: {name: workers-cp, managed_scaling: {target_capacity: 80}}
"""


def _stack(write_env, text=FARGATE):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    network = NetworkStack(app, "test-network", environment=environment, env=env)
    deps = cdk.Stack(app, "deps", env=env)
    vpc = network.network.vpc
    group = ec2.SecurityGroup(deps, "App", vpc=vpc)
    roles = {
        name: iam.Role(deps, name, assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        for name in ("task", "execution")
    }
    profile_role = iam.Role(deps, "Instance", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))
    profile = iam.CfnInstanceProfile(deps, "Profile", roles=[profile_role.role_name])
    target_group = elbv2.ApplicationTargetGroup(
        deps, "Tg", vpc=vpc, port=3000, protocol=elbv2.ApplicationProtocol.HTTP,
        target_type=elbv2.TargetType.IP,
    )
    return EcsStack(
        app, "test-ecs", environment=environment, env=env,
        props=EcsProps.from_dict(yaml.safe_load(text)),
        network=network.network,
        security_groups={"app": group},
        roles=roles,
        instance_profiles={"ecs": profile},
        target_groups={"backend": target_group},
        repositories={"backend": ecr.Repository(deps, "Repo")},
        secrets={"db": secretsmanager.Secret(deps, "Db")},
    )


def test_fargate_task_and_service(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource_properties("AWS::ECS::Cluster", {
        "ClusterName": "int-production-use1-test-app",
        "ClusterSettings": [{"Name": "containerInsights", "Value": "enabled"}],
    })
    task = next(iter(template.find_resources("AWS::ECS::TaskDefinition").values()))["Properties"]
    assert task["Family"] == "int-production-use1-test-backend"
    assert task["NetworkMode"] == "awsvpc" and task["Cpu"] == "512"
    container = task["ContainerDefinitions"][0]
    assert container["PortMappings"] == [{"ContainerPort": 3000, "Protocol": "tcp"}]
    assert container["Environment"] == [{"Name": "NODE_ENV", "Value": "production"}]
    names = [secret["Name"] for secret in container["Secrets"]]
    assert names == ["JWT", "DB_PASSWORD", "DB_USERNAME", "DB_PORT", "DB_NAME"]
    assert ":v1" in json.dumps(container["Image"])
    assert container["LogConfiguration"]["LogDriver"] == "awslogs"
    assert container["LogConfiguration"]["Options"]["awslogs-stream-prefix"] == "backend"
    template.has_resource("AWS::Logs::LogGroup", {"DeletionPolicy": "Retain"})
    template.has_resource_properties("AWS::ECS::Service", {
        "ServiceName": "int-production-use1-test-backend",
        "DesiredCount": 2,
        "CapacityProviderStrategy": [{"CapacityProvider": "FARGATE", "Weight": 1, "Base": 0}],
        "LoadBalancers": [Match.object_like({"ContainerName": "backend", "ContainerPort": 3000})],
        "NetworkConfiguration": {"AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "ENABLED"})},
    })
    outputs = set(template.find_outputs("*"))
    assert {"AppClusterArn", "BackendServiceArn", "BackendTaskDefinitionArn"} <= outputs


def test_ec2_capacity_provider(write_env):
    template = Template.from_stack(_stack(write_env, EC2))
    template.has_resource_properties("AWS::EC2::LaunchTemplate", {
        "LaunchTemplateData": Match.object_like({
            "InstanceType": "t3.medium",
            "MetadataOptions": Match.object_like({"HttpTokens": "required"}),
        }),
    })
    template.resource_count_is("AWS::AutoScaling::AutoScalingGroup", 1)
    template.has_resource_properties("AWS::ECS::CapacityProvider", {
        "AutoScalingGroupProvider": Match.object_like({
            "ManagedScaling": Match.object_like({"TargetCapacity": 80}),
        }),
    })
    template.resource_count_is("AWS::ECS::ClusterCapacityProviderAssociations", 1)


@pytest.mark.parametrize("change,message", [
    (("NODE_ENV", "DB_PASSWORD"), "looks like a secret"),
    (("[FARGATE]}\n  task_definitions", "[EC2]}\n  task_definitions"), "unknown capacity provider"),
    (("container_port: 3000}]\n    network", "container_port: 4000}]\n    network"), "does not map port"),
])
def test_invalid_config_is_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        EcsProps.from_dict(yaml.safe_load(FARGATE.replace(*change, 1)))


def test_unknown_references_fail(write_env):
    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _stack(write_env, FARGATE.replace("repository: backend", "repository: missing"))


def test_empty_config_builds_nothing():
    assert not EcsProps.from_dict(None)
