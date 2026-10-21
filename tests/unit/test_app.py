"""The app entrypoint: how the environment name is resolved.

`app.py` is a script rather than a module, so these run it as a subprocess the
same way the `cdk` CLI does. The CLI passes `--context` through the
`CDK_CONTEXT_JSON` variable and the output directory through `CDK_OUTDIR`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cognitech_cdk.common.environment import load_environment

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(outdir: Path, *, env_var: str | None = None, context: str | None = None):
    env = {k: v for k, v in os.environ.items() if k not in ("ENV", "CDK_CONTEXT_JSON")}
    env["JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION"] = "1"
    env["CDK_OUTDIR"] = str(outdir)
    if env_var is not None:
        env["ENV"] = env_var
    if context is not None:
        # This is how `cdk --context env=<name>` reaches the app.
        env["CDK_CONTEXT_JSON"] = json.dumps({"env": context})

    return subprocess.run(
        [sys.executable, "app.py"], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )


def _stacks(outdir: Path) -> set[str]:
    return {
        p.name.removesuffix(".template.json") for p in outdir.glob("*.template.json")
        if not p.name.endswith(".nested.template.json")  # nested stacks deploy with their parent
    }


STACKS = (
    ("security_groups", "security"), ("iam", "iam"), ("transit_gateway", "transit-gateway"),
    ("storage", "storage"), ("key_pairs", "key-pairs"), ("secrets", "secrets"), ("ssm", "ssm"),
    ("certificates", "certificates"), ("backups", "backup"), ("load_balancing", "load-balancing"),
    ("waf", "waf"), ("ecr", "ecr"), ("eks", "eks"), ("ecs", "ecs"),
    ("rds", "rds"), ("opensearch", "opensearch"), ("firehose", "firehose"), ("lambdas", "lambda"),
    ("events", "events"), ("cognito", "cognito"),
)


def _expected_stacks(env_name: str) -> set[str]:
    """Build the stack names from the config, so edits to env.yaml do not break these."""
    environment = load_environment(env_name)
    names = {environment.common.name(environment.network.name, "network")}
    for attribute, suffix in STACKS:
        if getattr(environment, attribute):
            names.add(environment.common.name(environment.network.name, suffix))
    return names


def test_defaults_to_dev_when_nothing_is_set(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _stacks(tmp_path) == _expected_stacks("dev")


def test_dev_system_nodes_core_addons_and_public_provider_networking(tmp_path):
    result = _run(tmp_path, context="dev")
    assert result.returncode == 0, result.stderr
    environment = load_environment("dev")
    stack_name = environment.common.name(environment.network.name, "eks")
    resources = json.loads((tmp_path / f"{stack_name}.template.json").read_text())["Resources"]
    clusters = [resource for resource in resources.values() if resource["Type"] == "AWS::EKS::Cluster"]
    assert len(clusters) == 1
    config = clusters[0]["Properties"]["ResourcesVpcConfig"]
    assert config["EndpointPublicAccess"] is True
    assert config["EndpointPrivateAccess"] is False
    providers = {
        logical_id: resource["Properties"]
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::Lambda::Function" and "KubectlProvider" in logical_id
    }
    assert len(providers) == 2
    assert {props["Handler"] for props in providers.values()} == {"index.handler", "framework.onEvent"}
    for logical_id, props in providers.items():
        assert "VpcConfig" not in props, f"{logical_id} must not be attached to public subnets"

    all_resources = [
        resource
        for path in tmp_path.glob("*.template.json")
        for resource in json.loads(path.read_text())["Resources"].values()
    ]
    nodes = [
        resource["Properties"] for resource in all_resources
        if resource["Type"] == "AWS::EKS::Nodegroup"
    ]
    assert len(nodes) == 1
    assert nodes[0]["InstanceTypes"] == ["t3.large"]
    assert nodes[0]["ScalingConfig"] == {"MinSize": 2, "DesiredSize": 2, "MaxSize": 3}
    assert nodes[0]["Labels"] == {"workload-type": "system"}
    assert nodes[0]["Taints"] == [
        {"Key": "workload-type", "Value": "system", "Effect": "NO_SCHEDULE"}
    ]
    assert "LaunchTemplate" in nodes[0]
    templates = [
        resource["Properties"]["LaunchTemplateData"] for resource in all_resources
        if resource["Type"] == "AWS::EC2::LaunchTemplate"
    ]
    assert len(templates) == 1
    assert len(templates[0]["SecurityGroupIds"]) == 2
    disk = templates[0]["BlockDeviceMappings"][0]["Ebs"]
    assert disk["VolumeSize"] == 20
    assert disk["VolumeType"] == "gp3"
    assert disk["Encrypted"] is True
    addons = {
        resource["Properties"]["AddonName"]: resource["Properties"]
        for resource in all_resources if resource["Type"] == "AWS::EKS::Addon"
    }
    assert addons.keys() == {
        "vpc-cni", "coredns", "kube-proxy", "eks-pod-identity-agent",
        "aws-ebs-csi-driver", "metrics-server",
    }
    assert addons["aws-ebs-csi-driver"]["PodIdentityAssociations"][0]["ServiceAccount"] == "ebs-csi-controller-sa"
    charts = [
        resource["Properties"] for resource in all_resources
        if resource["Type"] == "Custom::AWSCDK-EKS-HelmChart"
    ]
    assert [chart["Chart"] for chart in charts] == ["aws-load-balancer-controller"]
    subnets = [
        resource["Properties"] for resource in all_resources
        if resource["Type"] == "AWS::EC2::Subnet"
    ]
    assert len(subnets) == 3
    assert all(subnet["MapPublicIpOnLaunch"] is True for subnet in subnets)
    manifests = [
        obj for resource in all_resources
        if resource["Type"] == "Custom::AWSCDK-EKS-KubernetesResource"
        for obj in json.loads(resource["Properties"]["Manifest"])
    ]
    storage = next(obj for obj in manifests if obj["kind"] == "StorageClass")
    assert storage["metadata"]["name"] == "gp3"
    assert storage["metadata"]["annotations"]["storageclass.kubernetes.io/is-default-class"] == "true"
    assert storage["parameters"]["encrypted"] == "true"
    assert not any(obj["kind"] in {"NodePool", "EC2NodeClass"} for obj in manifests)


def test_dev_eks_access_namespaces_and_healthpath_quota(tmp_path):
    configured_entries = {
        principal: entry
        for cluster in load_environment("dev").eks.clusters
        for entry in cluster.access_entries
        for principal in entry.principal_arns
    }
    result = _run(tmp_path, context="dev")
    assert result.returncode == 0, result.stderr
    resources = [
        resource
        for path in tmp_path.glob("*.template.json")
        for resource in json.loads(path.read_text())["Resources"].values()
    ]
    entries = [
        resource["Properties"] for resource in resources
        if resource["Type"] == "AWS::EKS::AccessEntry"
    ]
    principal_entries = {
        entry["PrincipalArn"]: entry
        for entry in entries
        if isinstance(entry["PrincipalArn"], str)
    }
    assert configured_entries
    assert principal_entries.keys() == configured_entries.keys()
    assert len(principal_entries) == sum(
        isinstance(entry["PrincipalArn"], str) for entry in entries
    )
    for principal, configured in configured_entries.items():
        entry = principal_entries[principal]
        assert entry["Type"] == "STANDARD"
        assert entry.get("KubernetesGroups", []) == list(configured.kubernetes_groups)
        scope = (
            {"Type": "namespace", "Namespaces": list(configured.namespaces)}
            if configured.namespaces else {"Type": "cluster"}
        )
        assert entry.get("AccessPolicies", []) == [
            {
                "PolicyArn": policy if policy.startswith("arn:") else {
                    "Fn::Join": ["", [
                        "arn:", {"Ref": "AWS::Partition"},
                        f":eks::aws:cluster-access-policy/{policy}",
                    ]],
                },
                "AccessScope": scope,
            }
            for policy in configured.policy_arns
        ]

    objects = [
        obj
        for resource in resources
        if resource["Type"] == "Custom::AWSCDK-EKS-KubernetesResource"
        for obj in json.loads(resource["Properties"]["Manifest"])
    ]
    view_binding = next(
        obj for obj in objects
        if obj["kind"] == "ClusterRoleBinding" and obj["metadata"]["name"] == "view-binding"
    )
    assert view_binding["roleRef"]["name"] == "cognitech-view"
    assert view_binding["subjects"] == [{
        "kind": "Group", "name": "cognitech-viewers",
        "apiGroup": "rbac.authorization.k8s.io",
    }]
    namespaces = {
        obj["metadata"]["name"]: obj["metadata"]["labels"]
        for obj in objects if obj["kind"] == "Namespace"
    }
    expected_namespaces = {
        "infogrid": {"team": "infogrid-devops"},
        "movienight": {"app": "movienight"},
        "healthpath": {"app": "healthpath"},
        "pulsehub": {"app": "pulsehub"},
    }
    assert namespaces.keys() == expected_namespaces.keys()
    for namespace, labels in expected_namespaces.items():
        assert labels.items() <= namespaces[namespace].items()
    quotas = [obj for obj in objects if obj["kind"] == "ResourceQuota"]
    assert len(quotas) == 1
    quota = quotas[0]
    assert quota["apiVersion"] == "v1"
    assert quota["metadata"]["name"] == "healthpath-quota"
    assert quota["metadata"]["namespace"] == "healthpath"
    assert quota["metadata"]["labels"]["app"] == "healthpath"
    assert quota["spec"] == {"hard": {
        "requests.cpu": "4", "requests.memory": "8Gi",
        "limits.cpu": "8", "limits.memory": "16Gi", "pods": "20",
    }}


def test_env_variable_selects_the_environment(tmp_path):
    result = _run(tmp_path, env_var="prod")

    assert result.returncode == 0, result.stderr
    assert _stacks(tmp_path) == _expected_stacks("prod")


def test_context_overrides_the_variable(tmp_path):
    """--context is the more explicit signal, so it must win."""
    result = _run(tmp_path, env_var="prod", context="dev")

    assert result.returncode == 0, result.stderr
    assert _stacks(tmp_path) == _expected_stacks("dev")


@pytest.mark.parametrize("kwargs", [{"env_var": "staging"}, {"context": "staging"}])
def test_unknown_environment_fails_with_a_helpful_message(tmp_path, kwargs):
    result = _run(tmp_path, **kwargs)

    assert result.returncode != 0
    assert "Unknown environment 'staging'" in result.stderr
    assert "dev, prod" in result.stderr
    assert "ENV" in result.stderr
