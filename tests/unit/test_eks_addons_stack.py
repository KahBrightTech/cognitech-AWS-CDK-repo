"""EKS add-ons, Karpenter and Helm apps, built as nested stacks of the EKS stack."""

import json

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.eks import EksProps
from cognitech_cdk.constructs.eks_addons import NATIVE_ADDONS, EksAddonsProps
from cognitech_cdk.stacks.eks_stack import EksStack
from cognitech_cdk.stacks.network_stack import NetworkStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16\nazs: 2"
EKS = """
- name: main
  role: eks-cluster
  subnet_tier: public
  node_groups:
  - {name: general, role: eks-nodes, subnet_tier: public, instance_types: [t3.medium], min_size: 1, desired_size: 1, max_size: 2}
"""
ADDONS = """
- cluster: main
  addons:
  - name: vpc-cni
  - name: coredns
  - name: kube-proxy
  - name: eks-pod-identity-agent
  - name: aws-ebs-csi-driver
  - name: aws-efs-csi-driver
  - name: aws-fsx-csi-driver
  - name: aws-secrets-store-csi-driver-provider
  - name: metrics-server
  - name: external-dns
    hosted_zone_ids: [Z0123456789ABC]
  - name: cert-manager
  - name: amazon-cloudwatch-observability
    configuration_values: {containerLogs: {enabled: true}}
  aws_load_balancer_controller: {enabled: true}
  helm_charts:
  - name: ingress-nginx
    chart: ingress-nginx
    repository: https://kubernetes.github.io/ingress-nginx
    version: 4.11.2
"""
TYPED = """
- cluster: main
  addons:
  - name: vpc-cni
  - name: coredns
  - name: kube-proxy
  - name: eks-pod-identity-agent
  - name: aws-ebs-csi-driver
  aws_load_balancer_controller: {enabled: true}
  storage_class: {name: gp3}
  load_balancer_defaults:
    certificate: dev
    security_groups: [alb]
    group_name: shared
  karpenter:
    node_role: eks-nodes
    replicas: 1
    security_groups: [alb]
    node_pool: {instance_categories: [t, m], cpu_limit: 8}
  argocd:
    host: argocd.dev.example.com
  nginx_ingress:
  - name: public
    replica_count: 1
  gateway_api:
    service: {}
  kubecost: {}
  awx_operator:
    create_instance: true
    host: awx.dev.example.com
"""


def _stacks(write_env, addons=ADDONS, eks=EKS):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    network = NetworkStack(app, "test-network", environment=environment, env=env)
    deps = cdk.Stack(app, "deps", env=env)
    roles = {
        "eks-cluster": iam.Role(deps, "Cluster", assumed_by=iam.ServicePrincipal("eks.amazonaws.com")),
        "eks-nodes": iam.Role(deps, "Nodes", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com")),
    }
    groups = {"alb": ec2.SecurityGroup(deps, "Alb", vpc=network.network.vpc)}
    certificate = acm.Certificate(deps, "Cert", domain_name="*.dev.example.com")
    groups["x"] = ec2.SecurityGroup(deps, "Sg", vpc=network.network.vpc)
    groups["eks-nodes"] = ec2.SecurityGroup(deps, "NodesSg", vpc=network.network.vpc)
    eks_stack = EksStack(
        app, "test-eks", environment=environment, env=env,
        props=EksProps.from_dict(yaml.safe_load(eks), base_dir=_repo_root()), network=network.network,
        security_groups=groups, roles=roles,
        key_pairs={"key-pair": ec2.KeyPair.from_key_pair_name(deps, "KeyPair", "test-key-pair")},
        addons=EksAddonsProps.from_dict(yaml.safe_load(addons), base_dir=_repo_root()),
        certificates={"dev": certificate},
    )
    return eks_stack, _addons(eks_stack)


def _addons(eks_stack, cluster="main"):
    """All add-on nested stacks of one cluster, read as a single template."""
    resources = {}
    for group, nested in eks_stack.nested_stacks[cluster].items():
        if group in ("CoreAddons", "Karpenter", "Addons", "Apps"):
            resources.update(Template.from_stack(nested).to_json()["Resources"])
    return Template.from_json({"Resources": resources})


def _nested_order(eks_stack, cluster="main"):
    """Nested stack group -> the groups it depends on, from the parent template."""
    template = Template.from_stack(eks_stack).to_json()["Resources"]
    ids = {eks_stack.get_logical_id(stack.nested_stack_resource): group
           for group, stack in eks_stack.nested_stacks[cluster].items()}
    return {
        ids[key]: {ids[d] for d in resource.get("DependsOn", []) if d in ids}
        for key, resource in template.items() if key in ids
    }


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


def test_dev_system_and_optional_application_scheduling(write_env):
    text = (_repo_root() / "deployments/dev/env.yaml").read_text()
    active = yaml.safe_load(text)
    assert active["eks"][0]["compute"]["node_groups"][0]["desired_size"] == 2
    assert "karpenter" not in active["eks"][0]["compute"]
    compute_text = text.split("    compute:\n", 1)[1].split("\neks_addons:\n", 1)[0]
    compute = yaml.safe_load(
        "compute:\n" + "\n".join(
            line.removeprefix("    # ") if line.startswith("    # ") else line.removeprefix("    ")
            for line in compute_text.splitlines()
        )
    )["compute"]
    addons_text = text.split("\neks_addons:\n", 1)[1]
    assert "CriticalAddonsOnly" not in addons_text
    addons = yaml.safe_load("\n".join(line.removeprefix("# ") for line in addons_text.splitlines()))
    # The native Secrets Store add-on is an alternative to the enabled Helm bundle.
    addons[0]["addons"] = [
        item for item in addons[0]["addons"]
        if item["name"] != "aws-secrets-store-csi-driver-provider"
    ]
    eks = yaml.safe_load(EKS)
    del eks[0]["node_groups"]
    eks[0]["compute"] = compute
    stack, template = _stacks(write_env, yaml.safe_dump(addons), yaml.safe_dump(eks))
    system_selector = {"workload-type": "system"}
    system_toleration = {
        "key": "workload-type", "operator": "Equal", "value": "system", "effect": "NoSchedule",
    }
    nodes = Template.from_stack(stack.nested_stacks["main"]["NodeGroups"])
    nodes.has_resource_properties("AWS::EKS::Nodegroup", {
        "Labels": system_selector,
        "Taints": [{"Key": "workload-type", "Value": "system", "Effect": "NO_SCHEDULE"}],
    })
    native = {
        resource["Properties"]["AddonName"]: json.loads(resource["Properties"].get("ConfigurationValues", "{}"))
        for resource in template.find_resources("AWS::EKS::Addon").values()
    }

    def assert_system(settings):
        assert settings["nodeSelector"] == system_selector
        assert system_toleration in settings["tolerations"]

    for name in ("coredns", "metrics-server", "external-dns", "cert-manager"):
        assert_system(native[name])
    for name in ("aws-ebs-csi-driver", "aws-efs-csi-driver", "aws-fsx-csi-driver"):
        assert_system(native[name]["controller"])
        assert "nodeSelector" not in native[name]["node"]
    assert native["aws-ebs-csi-driver"]["node"]["tolerateAllTaints"] is True
    for name in ("aws-efs-csi-driver", "aws-fsx-csi-driver"):
        assert native[name]["node"]["tolerations"] == [{"operator": "Exists"}]
    for name in ("vpc-cni", "kube-proxy"):
        assert native[name] == {}
    assert native["eks-pod-identity-agent"] == {"tolerations": [{"operator": "Exists"}]}
    for component in ("cainjector", "webhook"):
        assert_system(native["cert-manager"][component])
    cloudwatch = native["amazon-cloudwatch-observability"]
    for component in ("manager", "kubeStateMetrics"):
        assert_system(cloudwatch[component])
    assert cloudwatch["tolerations"] == [{"operator": "Exists"}]
    assert "nodeSelector" not in cloudwatch
    charts = {
        name: json.loads(_values(chart)) for name, chart in _charts(template).items()
        if "Values" in chart["Properties"]
    }
    secrets = charts["secrets-store-csi-driver-provider-aws"]
    assert secrets["tolerations"] == [{"operator": "Exists"}]
    assert secrets["secrets-store-csi-driver"]["linux"]["tolerations"] == [{"operator": "Exists"}]
    assert "nodeSelector" not in secrets
    for name in ("karpenter", "aws-load-balancer-controller", "kubecost"):
        assert_system(charts[name])
    assert_system(charts["argocd"]["global"])
    assert charts["argocd"]["redis-ha"]["enabled"] is False
    assert_system(charts["nginx-ingress-public"]["controller"])
    gateway = charts["nginx-gateway-fabric"]
    assert_system(gateway["nginxGateway"])
    assert_system(gateway["nginx"]["pod"])
    assert_system(gateway["certGenerator"])
    kubecost = charts["kubecost"]
    for name in ("forecasting", "finopsagent", "grafana"):
        assert_system(kubecost[name])
    for name in ("server", "alertmanager"):
        assert_system(kubecost["prometheus"][name])
    for settings in (kubecost["networkCosts"], kubecost["prometheus"]["nodeExporter"]):
        assert "nodeSelector" not in settings
        assert settings["tolerations"] == [{"operator": "Exists"}]
    awx = charts["awx-operator"]
    assert_system(awx["operator-controller"]["spec"]["template"]["spec"])
    for key in ("node_selector", "postgres_selector"):
        assert yaml.safe_load(awx["AWX"]["spec"][key]) == {"karpenter.sh/nodepool": "default"}


def test_every_requested_native_addon_is_separate(write_env):
    eks_stack, template = _stacks(write_env)
    found = {
        resource["Properties"]["AddonName"]
        for resource in template.find_resources("AWS::EKS::Addon").values()
    }
    assert found == set(NATIVE_ADDONS)
    Template.from_stack(eks_stack).resource_count_is("AWS::EKS::Addon", 0)
    core = Template.from_stack(eks_stack.nested_stacks["main"]["CoreAddons"]).find_resources("AWS::EKS::Addon")
    assert {r["Properties"]["AddonName"] for r in core.values()} == {
        "vpc-cni", "coredns", "kube-proxy", "eks-pod-identity-agent",
    }
    assert _nested_order(eks_stack)["CoreAddons"] == {"NodeGroups"}


def test_service_account_generated_pod_identity_role(write_env):
    raw = yaml.safe_load(EKS)
    raw[0]["namespaces"] = [{"name": "apps"}]
    raw[0]["service_accounts"] = [{
        "name": "reader", "namespace": "apps", "identity": "pod_identity",
        "policy": {
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                "Resource": "arn:aws:secretsmanager:us-east-1:111122223333:secret:example-abcdef",
            }],
        },
    }]
    stack, _ = _stacks(write_env, eks=yaml.safe_dump(raw))
    access = Template.from_stack(stack.nested_stacks["main"]["Access"])
    access.has_resource_properties("AWS::EKS::PodIdentityAssociation", {
        "Namespace": "apps", "ServiceAccount": "reader", "RoleArn": Match.any_value(),
    })
    access.has_resource_properties("AWS::IAM::Role", {
        "AssumeRolePolicyDocument": {"Statement": [Match.object_like({
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Principal": {"Service": "pods.eks.amazonaws.com"},
        })]},
    })
    association = next(iter(access.find_resources("AWS::EKS::PodIdentityAssociation").values()))
    assert association["DependsOn"]
    resources = json.dumps(access.find_resources("Custom::AWSCDK-EKS-KubernetesResource"))
    assert "eks.amazonaws.com/role-arn" not in resources


def test_secrets_store_helm_and_secret_provider_class_ordering(write_env):
    raw = yaml.safe_load(TYPED)
    raw[0]["secrets_store_csi"] = {}
    raw[0]["secret_provider_classes"] = [{
        "name": "app-secrets", "namespace": "apps",
        "parameters": {"usePodIdentity": "true", "objects": '- objectName: example\n  objectType: secretsmanager\n'},
    }]
    cluster = yaml.safe_load(EKS)
    cluster[0]["oidc_provider"] = True
    cluster[0]["namespaces"] = [{"name": "apps"}]
    cluster[0]["service_accounts"] = [
        {"name": "reader", "namespace": "apps", "identity": "pod_identity"},
        {"name": "reader-irsa", "namespace": "apps", "identity": "irsa"},
    ]
    stack, template = _stacks(write_env, addons=yaml.safe_dump(raw), eks=yaml.safe_dump(cluster))
    chart = _charts(template)["secrets-store-csi-driver-provider-aws"]
    assert chart["Properties"]["Version"] == "3.1.4"
    assert chart["Properties"]["Wait"] is True
    values = json.loads(_values(chart))
    assert values["secrets-store-csi-driver"]["install"] is True
    assert values["tolerations"] == [{"operator": "Exists"}]
    assert values["secrets-store-csi-driver"]["linux"]["tolerations"] == [{"operator": "Exists"}]
    resources = Template.from_stack(stack.nested_stacks["main"]["Addons"]).find_resources(
        "Custom::AWSCDK-EKS-KubernetesResource"
    )
    spc = next(r for r in resources.values() if "SecretProviderClass" in r["Properties"]["Manifest"])
    obj = json.loads(spc["Properties"]["Manifest"])[0]
    assert obj["spec"]["provider"] == "aws"
    assert obj["spec"]["parameters"]["usePodIdentity"] == "true"
    assert any("SecretsStoreCsi" in dependency for dependency in spc["DependsOn"])
    assert "Access" in _nested_order(stack)["Addons"]
    assert obj["metadata"]["namespace"] == "apps"


@pytest.mark.parametrize("settings, message", [
    ({"secrets_store_csi": {}, "addons": [{"name": "aws-secrets-store-csi-driver-provider"}]},
     "not both"),
    ({"secret_provider_classes": [{"name": "example", "parameters": {"objects": "[]"}}]},
     "need secrets_store_csi"),
    ({"secrets_store_csi": {"values": {"secrets-store-csi-driver": {"install": False}}}},
     "must stay enabled"),
    ({"secrets_store_csi": {}, "secret_provider_classes": [{"name": "example", "parameters": {}}]},
     "parameters.objects"),
])
def test_invalid_secrets_store_configuration(settings, message):
    with pytest.raises(ValueError, match=message):
        EksAddonsProps.from_dict([{
            "cluster": "main", "aws_load_balancer_controller": {"enabled": False}, **settings,
        }])


def test_native_secrets_provider_can_create_irsa_class(write_env):
    raw = yaml.safe_load(ADDONS)
    raw[0]["secret_provider_classes"] = [{
        "name": "app-secrets",
        "parameters": {"usePodIdentity": "false", "objects": '- objectName: example\n  objectType: secretsmanager\n'},
    }]
    _, template = _stacks(write_env, addons=yaml.safe_dump(raw))
    manifests = template.find_resources("Custom::AWSCDK-EKS-KubernetesResource")
    spc = next(r for r in manifests.values() if "SecretProviderClass" in r["Properties"]["Manifest"])
    assert any("AwsSecretsStoreCsiDriverProviderAddon" in dependency for dependency in spc["DependsOn"])


def test_aws_addons_use_pod_identity_roles(write_env):
    eks_stack, template = _stacks(write_env)
    addons = {
        resource["Properties"]["AddonName"]: resource
        for resource in template.find_resources("AWS::EKS::Addon").values()
    }
    for name in ("aws-ebs-csi-driver", "external-dns", "amazon-cloudwatch-observability"):
        association = addons[name]["Properties"]["PodIdentityAssociations"][0]
        assert association["ServiceAccount"] == NATIVE_ADDONS[name].service_account
    assert "PodIdentityAssociations" not in addons["cert-manager"]["Properties"]
    assert json.loads(addons["amazon-cloudwatch-observability"]["Properties"]["ConfigurationValues"]) == {
        "containerLogs": {"enabled": True}
    }
    template.has_resource_properties("AWS::IAM::Role", {
        "AssumeRolePolicyDocument": {"Statement": [Match.object_like({
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Principal": {"Service": "pods.eks.amazonaws.com"},
        })]},
    })
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "hostedzone/Z0123456789ABC" in policies
    # Pod Identity add-ons wait for the agent, which is in the CoreAddons stack.
    assert "CoreAddons" in _nested_order(eks_stack)["Addons"]


def test_load_balancer_controller_uses_helm_and_pod_identity(write_env):
    template = _stacks(write_env)[1]
    template.has_resource_properties("AWS::EKS::PodIdentityAssociation", {
        "Namespace": "kube-system",
        "ServiceAccount": "aws-load-balancer-controller",
    })
    charts = {
        resource["Properties"]["Chart"]: resource["Properties"]
        for resource in template.find_resources("Custom::AWSCDK-EKS-HelmChart").values()
    }
    assert charts["aws-load-balancer-controller"]["Version"] == "3.6.0"
    assert charts["aws-load-balancer-controller"]["Repository"] == "https://aws.github.io/eks-charts"
    assert charts["ingress-nginx"]["Version"] == "4.11.2"
    assert "elasticloadbalancing:CreateLoadBalancer" in json.dumps(
        template.find_resources("AWS::IAM::Policy")
    )


@pytest.mark.parametrize("text,message", [
    ("- cluster: main\n  addons: [{name: aws-ebs-csi-driver}]\n", "eks-pod-identity-agent"),
    ("- cluster: main\n  addons: [{name: karpenter}]\n", "not a supported native add-on"),
    ("- cluster: main\n  addons: [{name: coredns, pod_identity: true}]\n", "does not use AWS credentials"),
    ("- cluster: main\n  helm_charts: [{name: x, chart: x, repository: http://x, version: 1.0.0}]\n",
     "https:// or oci://"),
    ("- cluster: main\n  addons: [{name: external-dns, hosted_zone_ids: [bad]}]\n", "hosted zone"),
])
def test_invalid_config_is_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        EksAddonsProps.from_dict(yaml.safe_load(text), base_dir=_repo_root())


def test_unknown_cluster_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'other'"):
        _stacks(write_env, ADDONS.replace("cluster: main", "cluster: other"))


def test_empty_config_builds_nothing():
    assert not EksAddonsProps.from_dict(None)


def _charts(template):
    return {
        resource["Properties"].get("Release"): resource
        for resource in template.find_resources("Custom::AWSCDK-EKS-HelmChart").values()
    }


def _values(chart):
    """Chart values JSON with CloudFormation tokens replaced by '<token>'."""
    values = chart["Properties"]["Values"]
    if isinstance(values, str):
        return values
    return "".join(part if isinstance(part, str) else "<token>" for part in values["Fn::Join"][1])


@pytest.mark.parametrize("nested_compute", [False, True])
def test_karpenter_queue_rules_policy_and_ordering(write_env, nested_compute):
    addons = yaml.safe_load(TYPED)
    eks = yaml.safe_load(EKS)
    if nested_compute:
        eks[0]["compute"] = {
            "node_groups": eks[0].pop("node_groups"),
            "karpenter": addons[0].pop("karpenter"),
        }
    eks_stack, template = _stacks(write_env, yaml.safe_dump(addons), yaml.safe_dump(eks))
    template.has_resource_properties("AWS::SQS::Queue", {
        "MessageRetentionPeriod": 300, "SqsManagedSseEnabled": True,
    })
    template.resource_count_is("AWS::Events::Rule", 5)
    template.has_resource_properties("AWS::EKS::PodIdentityAssociation", {
        "Namespace": "kube-system", "ServiceAccount": "karpenter",
    })
    policy = next(
        r for r in template.find_resources("AWS::IAM::Policy").values()
        if r["Properties"].get("PolicyName") == "KarpenterController"
    )
    document = json.dumps(policy["Properties"]["PolicyDocument"])
    assert "Fn::Sub" in document and "kubernetes.io/cluster/${ClusterName}" in document
    charts = _charts(template)
    karpenter = charts["karpenter"]["Properties"]
    assert karpenter["Repository"] == "oci://public.ecr.aws/karpenter/karpenter"
    assert karpenter["Version"] == "1.15.0" and karpenter["Wait"] is True
    manifests = json.dumps(template.find_resources("Custom::AWSCDK-EKS-KubernetesResource"))
    assert "EC2NodeClass" in manifests and "al2023@latest" in manifests and "WhenEmptyOrUnderutilized" in manifests
    order = _nested_order(eks_stack)
    assert order == {
        "NodeGroups": set(), "CoreAddons": {"NodeGroups"}, "Karpenter": {"CoreAddons"},
        "Addons": {"CoreAddons", "Karpenter"}, "Apps": {"CoreAddons", "Karpenter", "Addons"},
    }


def test_duplicate_karpenter_configuration_is_rejected(write_env):
    eks = yaml.safe_load(EKS)
    eks[0]["compute"] = {"karpenter": yaml.safe_load(TYPED)[0]["karpenter"]}
    with pytest.raises(ValueError, match="configure karpenter under compute or eks_addons, not both"):
        _stacks(write_env, TYPED, yaml.safe_dump(eks))


def test_compute_karpenter_requires_pod_identity_agent(write_env):
    eks = yaml.safe_load(EKS)
    eks[0]["compute"] = {"karpenter": yaml.safe_load(TYPED)[0]["karpenter"]}
    with pytest.raises(ValueError, match="eks-pod-identity-agent"):
        _stacks(write_env, "- cluster: main", yaml.safe_dump(eks))


def test_argocd_alb_ingress_and_dependent_ordering(write_env):
    eks_stack, template = _stacks(write_env, TYPED)
    charts = _charts(template)
    argocd = charts["argocd"]
    values = _values(argocd)
    for expected in (
        '"server.insecure":true', '"hostname":"argocd.dev.example.com"',
        '"alb.ingress.kubernetes.io/certificate-arn":"<token>"',
        '"alb.ingress.kubernetes.io/group.name":"shared"',
        '"alb.ingress.kubernetes.io/healthcheck-path":"/healthz"',
        '"alb.ingress.kubernetes.io/manage-backend-security-group-rules":"true"',
    ):
        assert expected in values, expected
    controller = next(k for k, r in template.find_resources("Custom::AWSCDK-EKS-HelmChart").items()
                      if r["Properties"]["Release"] == "aws-load-balancer-controller")
    assert controller in argocd["DependsOn"]
    assert "dex.config" not in values


def test_nginx_gateway_kubecost_awx_and_storage_class(write_env):
    eks_stack, template = _stacks(write_env, TYPED)
    charts = _charts(template)
    nginx = charts["nginx-ingress-public"]["Properties"]
    assert nginx["Repository"] == "oci://ghcr.io/nginx/charts/nginx-ingress"
    nginx_values = _values(charts["nginx-ingress-public"])
    assert "aws-load-balancer-nlb-target-type" in nginx_values and "aws-load-balancer-ssl-cert" in nginx_values
    assert '"httpsPort":{"enable":true,"port":443,"targetPort":80}' in nginx_values
    assert charts["gateway-api-crds"]["Properties"].get("ChartAssetURL")
    gateway = charts["nginx-gateway-fabric"]
    assert next(k for k, r in template.find_resources("Custom::AWSCDK-EKS-HelmChart").items()
                if r["Properties"]["Release"] == "gateway-api-crds") in gateway["DependsOn"]
    assert "StrategicMerge" in _values(gateway)
    assert "ingress" not in _values(charts["kubecost"])
    awx = _values(charts["awx-operator"])
    assert "awx.dev.example.com" in awx and "ingress_annotations" in awx
    manifests = json.dumps(template.find_resources("Custom::AWSCDK-EKS-KubernetesResource"))
    assert "is-default-class" in manifests and "ebs.csi.aws.com" in manifests


def test_argocd_sso_renders_dex_saml_connector(tmp_path):
    from cognitech_cdk.constructs.eks_addons import ArgoCdSsoProps
    (tmp_path / "cert.pem").write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    (tmp_path / "rbac.csv").write_text("# comment\ng, admin@example.com, role:admin\n")
    sso = ArgoCdSsoProps.from_dict(
        {"enabled": True, "sso_url": "https://portal.sso.us-east-1.amazonaws.com/saml/assertion/x",
         "ca_pem_file": "cert.pem", "rbac_policies_file": "rbac.csv"},
        "sso", base_dir=tmp_path,
    )
    assert sso.ca_data and sso.rbac_policies == "g, admin@example.com, role:admin"
    with pytest.raises(ValueError, match="sso_url"):
        ArgoCdSsoProps.from_dict({"enabled": True, "ca_pem_file": "cert.pem"}, "sso", base_dir=tmp_path)
    (tmp_path / "key.pem").write_text("-----BEGIN CERTIFICATE-----\n-----BEGIN PRIVATE KEY-----\n")
    with pytest.raises(ValueError, match="public certificate"):
        ArgoCdSsoProps.from_dict({"ca_pem_file": "key.pem"}, "sso", base_dir=tmp_path)


@pytest.mark.parametrize("text,message", [
    ("- cluster: main\n  argocd: {host: a.example.com}\n", "aws_load_balancer_controller"),
    ("- cluster: main\n  storage_class: {}\n", "aws-ebs-csi-driver"),
    ("- cluster: main\n  addons: [{name: eks-pod-identity-agent}]\n  karpenter: {}\n", "node_role"),
    ("- cluster: main\n  kubecost: {ingress: {}}\n", "ingress needs host"),
    ("- cluster: main\n  addons: [{name: eks-pod-identity-agent}]\n  aws_load_balancer_controller: {enabled: true}\n"
     "  nginx_ingress: [{name: a, ingress_class_name: x}, {name: b, ingress_class_name: x}]\n", "unique"),
])
def test_typed_addon_validation(text, message):
    with pytest.raises(ValueError, match=message):
        EksAddonsProps.from_dict(yaml.safe_load(text), base_dir=_repo_root())
