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


def test_dev_eks_access_namespaces_and_healthpath_quota(tmp_path):
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
    sso_prefix = "arn:aws:iam::271457809232:role/aws-reserved/sso.amazonaws.com/"
    sso_entries = {
        entry["PrincipalArn"].removeprefix(sso_prefix): entry
        for entry in entries
        if isinstance(entry["PrincipalArn"], str)
        and entry["PrincipalArn"].startswith(sso_prefix)
    }
    assert set(sso_entries) == {
        "AWSReservedSSO_AdministratorAccess_7f215b32fef26e47",
        "AWSReservedSSO_SystemAdministrator_7845d1ed8dcf680a",
        "AWSReservedSSO_ReadOnlyAccess_d2947b3732f4ad12",
        "AWSReservedSSO_NetworkAdministrator_b9b78eb5953b12dd",
    }
    for role in (
        "AWSReservedSSO_AdministratorAccess_7f215b32fef26e47",
        "AWSReservedSSO_SystemAdministrator_7845d1ed8dcf680a",
        "AWSReservedSSO_NetworkAdministrator_b9b78eb5953b12dd",
    ):
        entry = sso_entries[role]
        policy = "AmazonEKSViewPolicy" if "NetworkAdministrator" in role else "AmazonEKSClusterAdminPolicy"
        assert entry["AccessPolicies"] == [{
            "PolicyArn": f"arn:aws:eks::aws:cluster-access-policy/{policy}",
            "AccessScope": {"Type": "cluster"},
        }]
        if "NetworkAdministrator" in role:
            assert entry["KubernetesGroups"] == ["cognitech-viewers"]
        else:
            assert not entry.get("KubernetesGroups")
    readonly_entries = [
        entry for entry in entries
        if entry.get("KubernetesGroups") == ["cognitech-viewers", "infogrid"]
    ]
    assert len(readonly_entries) == 3
    assert sso_entries["AWSReservedSSO_ReadOnlyAccess_d2947b3732f4ad12"] in readonly_entries
    assert all(not entry.get("AccessPolicies") for entry in readonly_entries)
    assert all(entry["Type"] == "STANDARD" for entry in readonly_entries)

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
