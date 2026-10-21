"""EKS clusters, access entries, node groups and launch templates."""

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.eks import EksClusterProps, EksProps, ServiceAccountProps
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


@pytest.mark.parametrize("nested_compute", [False, True])
@pytest.mark.parametrize("role_key, entry", [
    ("role", {"roles": ["eks-nodes"]}),
    ("role_arn", {"principal_arns": ["arn:aws:iam::111122223333:role/workers"]}),
])
def test_node_roles_cannot_have_standard_access_entries(write_env, nested_compute, role_key, entry):
    raw = yaml.safe_load(EKS)
    cluster = raw[0]
    group = cluster["node_groups"][0]
    del group["role"]
    group[role_key] = (
        "eks-nodes" if role_key == "role" else "arn:aws:iam::111122223333:role/workers"
    )
    cluster["access_entries"]["readonly"] = {
        **entry, "kubernetes_groups": ["viewers"],
    }
    if nested_compute:
        cluster["compute"] = {"node_groups": cluster.pop("node_groups")}
    with pytest.raises(ValueError, match="managed node group roles must not have STANDARD"):
        _stack(write_env, yaml.safe_dump(raw))


def test_service_account_irsa_role_and_annotation(write_env):
    import json

    raw = yaml.safe_load(EKS)
    raw[0]["oidc_provider"] = True
    raw[0]["namespaces"] = [{"name": "apps"}]
    raw[0]["service_accounts"] = [{
        "name": "reader", "namespace": "apps", "identity": "irsa",
        "managed_policies": ["AmazonS3ReadOnlyAccess"],
        "policy": {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                           "Resource": "arn:aws:secretsmanager:us-east-1:111122223333:secret:example-abcdef"}],
        },
    }]
    stack = _stack(write_env, yaml.safe_dump(raw))
    access = Template.from_stack(stack.nested_stacks["main"]["Access"])
    access.resource_count_is("AWS::EKS::PodIdentityAssociation", 0)
    role = next(
        resource["Properties"] for resource in access.find_resources("AWS::IAM::Role").values()
        if "sts:AssumeRoleWithWebIdentity" in json.dumps(resource)
    )
    trust = role["AssumeRolePolicyDocument"]
    assert "sts:AssumeRoleWithWebIdentity" in json.dumps(trust)
    assert "sts:AssumeRole\"" not in json.dumps(trust)
    conditions = json.dumps(access.find_resources("Custom::AWSCDKCfnJson"))
    assert "system:serviceaccount:apps:reader" in conditions
    assert ":aud" in conditions and "sts.amazonaws.com" in conditions
    assert "Federated" in json.dumps(trust)
    assert "secretsmanager:GetSecretValue" in json.dumps(access.find_resources("AWS::IAM::Policy"))
    resources = access.find_resources("Custom::AWSCDK-EKS-KubernetesResource")
    service_accounts = next(value for value in resources.values() if "ServiceAccount" in json.dumps(value))
    assert "eks.amazonaws.com/role-arn" in json.dumps(service_accounts)
    assert service_accounts["DependsOn"]
    assert ("apps", "reader") in stack.eks_clusters["main"].service_account_roles


@pytest.mark.parametrize("account, extras, message", [
    ({"identity": "invalid"}, {}, "identity must be one of"),
    ({"identity": "irsa"}, {}, "IRSA needs oidc_provider"),
    ({"policy": {"Statement": [{}]}}, {}, "need identity"),
    ({"identity": "irsa", "policy": {}}, {"oidc_provider": True}, "IAM policy document"),
    ({"identity": "pod_identity", "annotations": {"eks.amazonaws.com/role-arn": "arn"}},
     {}, "remove the role-arn annotation"),
    ({"identity": "pod_identity"}, {"pod_identity_associations": [{
        "namespace": "default", "service_account": "reader",
        "role_arn": "arn:aws:iam::111122223333:role/reader",
    }]}, "one identity method"),
])
def test_invalid_service_account_identity(account, extras, message):
    with pytest.raises(ValueError, match=message):
        EksClusterProps.from_dict({
            "name": "main", "service_accounts": [{"name": "reader", **account}], **extras,
        })


def test_service_account_pod_identity_requires_agent(write_env):
    raw = yaml.safe_load(EKS)
    raw[0]["service_accounts"] = [{"name": "reader", "identity": "pod_identity"}]
    with pytest.raises(ValueError, match="needs the eks-pod-identity-agent"):
        _stack(write_env, yaml.safe_dump(raw))


def test_service_account_preserves_manual_annotations_and_loads_policy_file(tmp_path):
    account = ServiceAccountProps.from_dict({
        "name": "external",
        "annotations": {"eks.amazonaws.com/role-arn": "arn:aws:iam::111122223333:role/external"},
    }, "main")
    assert account.identity is None
    assert account.annotations["eks.amazonaws.com/role-arn"].endswith(":role/external")
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": "ssm:GetParameters",
        "Resource": "arn:aws:ssm:us-east-1:111122223333:parameter/app/*",
    }]}
    (tmp_path / "read-parameters.yaml").write_text(yaml.safe_dump(policy))
    generated = ServiceAccountProps.from_dict({
        "name": "reader", "identity": "irsa", "policy": "read-parameters.yaml",
    }, "main", base_dir=tmp_path)
    assert generated.policy == policy


@pytest.mark.parametrize("nested_compute", [False, True])
def test_compute_launch_template_and_node_group(write_env, nested_compute):
    raw = yaml.safe_load(EKS)
    cluster = raw[0]
    compute = {
        "launch_templates": [{
            "name": "general", "volume_size": 40, "security_groups": ["eks-cluster"],
        }],
        "node_groups": cluster.pop("node_groups"),
    }
    compute["node_groups"][0]["launch_template"] = "general"
    compute["node_groups"][0]["taints"] = [{
        "key": "dedicated", "value": "apps", "effect": "NO_SCHEDULE",
    }]
    if nested_compute:
        cluster["compute"] = compute
    else:
        cluster.update(compute)
    stack = _stack(write_env, yaml.safe_dump(raw))
    nodes = Template.from_stack(stack.nested_stacks["main"]["NodeGroups"])
    nodes.resource_count_is("AWS::EC2::LaunchTemplate", 1)
    nodes.has_resource_properties("AWS::EC2::LaunchTemplate", {
        "LaunchTemplateData": Match.object_like({
            "BlockDeviceMappings": Match.array_with([Match.object_like({
                "Ebs": Match.object_like({"VolumeSize": 40}),
            })]),
        }),
    })
    nodes.has_resource_properties("AWS::EKS::Nodegroup", {
        "LaunchTemplate": {"Id": Match.any_value(), "Version": Match.any_value()},
        "Labels": {"workload": "general"},
        "Taints": [{"Key": "dedicated", "Value": "apps", "Effect": "NO_SCHEDULE"}],
    })


@pytest.mark.parametrize("key", ["node_groups", "launch_templates"])
def test_duplicate_compute_configuration_is_rejected(key):
    with pytest.raises(ValueError, match=f"configure {key} directly or under compute, not both"):
        EksProps.from_dict([{"name": "main", key: [], "compute": {key: []}}])


@pytest.mark.parametrize("compute", [None, [], {"fargate": {}}])
def test_invalid_compute_configuration_is_rejected(compute):
    with pytest.raises(ValueError, match="compute"):
        EksProps.from_dict([{"name": "main", "compute": compute}])


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
