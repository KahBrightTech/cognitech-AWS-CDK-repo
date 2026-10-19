"""EKS clusters, access entries, node groups and launch templates."""

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.eks import EksClusterProps, EksProps
from cognitech_cdk.stacks.eks_stack import EksStack
from cognitech_cdk.stacks.network_stack import NetworkStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16\nazs: 2"
EKS = """
- name: main
  role: eks-cluster
  subnet_tier: public
  security_groups: [eks-cluster]
  logging: [api, audit]
  access_entries:
    admin:
      principal_arns: [arn:aws:iam::111122223333:role/admin]
      policy_arns: [AmazonEKSClusterAdminPolicy]
  node_groups:
  - name: general
    role: eks-nodes
    subnet_tier: public
    instance_types: [t3.medium]
    min_size: 1
    desired_size: 1
    max_size: 2
    labels: {workload: general}
"""


def _stack(write_env, text=EKS):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    network = NetworkStack(app, "test-network", environment=environment, env=env)
    deps = cdk.Stack(app, "deps", env=env)
    roles = {
        "eks-cluster": iam.Role(deps, "Cluster", assumed_by=iam.ServicePrincipal("eks.amazonaws.com")),
        "eks-nodes": iam.Role(deps, "Nodes", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com")),
    }
    groups = {"eks-cluster": ec2.SecurityGroup(deps, "Sg", vpc=network.network.vpc)}
    return EksStack(
        app, "test-eks", environment=environment, env=env,
        props=EksProps.from_dict(yaml.safe_load(text)),
        network=network.network, security_groups=groups, roles=roles,
    )


def test_cluster_uses_configured_role_subnets_and_version(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource_properties("AWS::EKS::Cluster", Match.object_like({
        "Name": "int-production-use1-test-main-eks-cluster",
        "Version": "1.36",
        "RoleArn": {"Fn::ImportValue": Match.any_value()},
        "ResourcesVpcConfig": Match.object_like({
            "SecurityGroupIds": [{"Fn::ImportValue": Match.any_value()}],
        }),
    }))
    cluster = next(iter(template.find_resources("AWS::EKS::Cluster").values()))["Properties"]
    assert sorted(item["Type"] for item in cluster["Logging"]["ClusterLogging"]["EnabledTypes"]) == ["api", "audit"]


@pytest.mark.parametrize(
    ("policy", "expected"),
    [(None, "Delete"), ("destroy", "Delete"), ("retain", "Retain")],
)
def test_cluster_removal_policy(write_env, policy, expected):
    text = EKS if policy is None else EKS + f"  removal_policy: {policy}\n"
    template = Template.from_stack(_stack(write_env, text))
    cluster = next(iter(template.find_resources("AWS::EKS::Cluster").values()))
    assert cluster["DeletionPolicy"] == expected
    assert cluster["UpdateReplacePolicy"] == expected


def test_direct_cluster_props_default_to_destroy():
    assert EksClusterProps(name="main").removal_policy == "destroy"


@pytest.mark.parametrize("kubernetes_groups", [None, ["cognitech-viewers"]])
def test_node_group_and_access_entry_are_nested(write_env, kubernetes_groups):
    text = EKS
    if kubernetes_groups is not None:
        text = text.replace(
            "      policy_arns: [AmazonEKSClusterAdminPolicy]",
            "      policy_arns: [AmazonEKSClusterAdminPolicy]\n"
            f"      kubernetes_groups: {kubernetes_groups}",
        )
    stack = _stack(write_env, text)
    nested = stack.nested_stacks["main"]
    assert set(nested) == {"Access", "NodeGroups"}
    parent = Template.from_stack(stack)
    parent.resource_count_is("AWS::CloudFormation::Stack", 2)
    parent.resource_count_is("AWS::EKS::Nodegroup", 0)
    nodes = Template.from_stack(nested["NodeGroups"])
    nodes.has_resource_properties("AWS::EKS::Nodegroup", Match.object_like({
        "InstanceTypes": ["t3.medium"],
        "AmiType": "AL2023_x86_64_STANDARD",
        "ScalingConfig": {"MinSize": 1, "DesiredSize": 1, "MaxSize": 2},
        "Labels": {"workload": "general"},
        "NodeRole": {"Fn::ImportValue": Match.any_value()},
        "ClusterName": {"Ref": Match.any_value()},
    }))
    access = Template.from_stack(nested["Access"])
    access.has_resource_properties("AWS::EKS::AccessEntry", Match.object_like({
        "PrincipalArn": "arn:aws:iam::111122223333:role/admin",
        "AccessPolicies": [Match.object_like({
            "PolicyArn": {"Fn::Join": ["", Match.array_with([
                ":eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
            ])]},
        })],
    }))
    entry = next(iter(access.find_resources("AWS::EKS::AccessEntry").values()))["Properties"]
    if kubernetes_groups is None:
        assert "KubernetesGroups" not in entry
    else:
        assert entry["KubernetesGroups"] == kubernetes_groups
    outputs = set(parent.find_outputs("*"))
    assert {"MainClusterName", "MainClusterArn", "MainGeneralNodeGroupName"} <= outputs


@pytest.mark.parametrize("change,expected", [
    (("instance_types: [t3.medium]", "instance_types: [t4g.medium]"), "AL2023_ARM_64_STANDARD"),
    (("instance_types: [t3.medium]", "instance_types: [t3.medium]\n    ami_type: BOTTLEROCKET_x86_64"),
     "BOTTLEROCKET_x86_64"),
])
def test_node_group_ami_type(write_env, change, expected):
    stack = _stack(write_env, EKS.replace(*change))
    nodes = Template.from_stack(stack.nested_stacks["main"]["NodeGroups"])
    nodes.has_resource_properties("AWS::EKS::Nodegroup", {"AmiType": expected})


def test_private_tier_requires_private_subnets(write_env):
    with pytest.raises(ValueError, match="private"):
        _stack(write_env, EKS.replace("subnet_tier: public\n  security", "subnet_tier: private\n  security"))


@pytest.mark.parametrize("change,message", [
    (("max_size: 2", "max_size: 0"), "max_size|desired_size"),
    (("role: eks-cluster", "role: eks-cluster\n  bogus: 1"), "unknown fields"),
])
def test_invalid_config_is_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        EksProps.from_dict(yaml.safe_load(EKS.replace(*change)))


def test_unknown_role_reference_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _stack(write_env, EKS.replace("role: eks-nodes", "role: missing"))


def test_cluster_without_node_groups(write_env):
    """The control plane can be created with subnets and SGs and no workers."""
    stack = _stack(write_env, """
- name: main
  role: eks-cluster
  subnet_tier: public
  security_groups: [eks-cluster]
""")
    assert stack.nested_stacks["main"] == {}
    assert stack.node_groups["main"] == {}
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::EKS::Cluster", 1)
    template.resource_count_is("AWS::EKS::Nodegroup", 0)
    template.resource_count_is("AWS::CloudFormation::Stack", 0)
    template.has_resource_properties("AWS::EKS::Cluster", Match.object_like({
        "ResourcesVpcConfig": Match.object_like({
            "SecurityGroupIds": [{"Fn::ImportValue": Match.any_value()}],
        }),
    }))
    assert "MainClusterName" in template.find_outputs("*")
    assert "MainGeneralNodeGroupName" not in template.find_outputs("*")
    assert "MainOidcProviderArn" not in template.find_outputs("*")


def test_oidc_provider_without_nodes(write_env):
    stack = _stack(write_env, """
- name: main
  role: eks-cluster
  subnet_tier: public
  security_groups: [eks-cluster]
  oidc_provider: true
""")
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::EKS::Nodegroup", 0)
    assert stack.eks_clusters["main"].oidc_provider is not None
    assert "MainOidcProviderArn" in template.find_outputs("*")
    assert "MainOidcIssuer" in template.find_outputs("*")


def test_empty_config_builds_nothing():
    assert not EksProps.from_dict(None)
