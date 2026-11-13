"""The stack dropdown in each deploy workflow is generated, not typed.

`scripts/sync_stack_choices.py` writes it from `cdk list`. These tests check the
rewriting itself, and that what is committed still matches what the app builds,
so a stack added to `app.py` without re-running the script fails the PR instead
of showing a stale form.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from cognitech_cdk.common.environment import list_environments, load_environment

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "sync_stack_choices.py"
SELECTION_SCRIPT = REPO_ROOT / "scripts" / "select_stacks.py"

DEPLOY_WORKFLOWS = sorted(WORKFLOWS.glob("deploy-*.yml"))


def _load_script(path=SCRIPT):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_script()
selection = _load_script(SELECTION_SCRIPT)


WORKFLOW = """\
on:
  workflow_dispatch:
    inputs:
      stacks:
        type: choice
        # BEGIN generated stack options -- scripts/sync_stack_choices.py
        options:
          - all
          - stale-stack
        # END generated stack options
jobs:
  cdk:
    env:
      ENV: dev
"""


def _options(text: str, begin=sync.BEGIN, end=sync.END) -> list[str]:
    """The stack names between the generated markers."""
    inside = text.split(begin)[1].split(end)[0]
    return [
        line.strip().removeprefix("- ")
        for line in inside.splitlines()
        if line.strip().startswith("- ")
    ]


def _built_stacks(env_name: str, outdir: Path) -> set[str]:
    """What `app.py` builds, without needing the cdk CLI (see test_app.py)."""
    env = {k: v for k, v in os.environ.items() if k not in ("ENV", "CDK_CONTEXT_JSON")}
    env["JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION"] = "1"
    env["CDK_OUTDIR"] = str(outdir)
    env["ENV"] = env_name

    result = subprocess.run(
        [sys.executable, "app.py"], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    return {
        p.name.removesuffix(".template.json") for p in outdir.glob("*.template.json")
        if not p.name.endswith(".nested.template.json")  # nested stacks deploy with their parent
    }


def test_render_replaces_the_options_and_nothing_else():
    updated = sync.render(WORKFLOW, ["one", "two"])

    assert _options(updated) == ["all", "one", "two"]
    assert "stale-stack" not in updated
    assert "ENV: dev" in updated
    # The indentation has to survive, or the workflow stops being valid YAML.
    assert "        options:\n          - all\n" in updated


def test_render_is_stable_when_nothing_changed():
    once = sync.render(WORKFLOW, ["one"])

    assert sync.render(once, ["one"]) == once


def test_render_refuses_a_file_without_markers():
    with pytest.raises(ValueError):
        sync.render("on: push\n", ["one"])


def test_render_updates_both_dropdowns_for_new_stacks():
    workflow = WORKFLOW.replace("jobs:\n", """\
      exclude:
        description: Skip one stack
        type: choice
        default: none
        # BEGIN generated exclude options -- scripts/sync_stack_choices.py
        options:
          - none
          - stale-exclusion
        # END generated exclude options
jobs:
""")
    updated = sync.render(workflow, ["one", "new-stack"])
    assert _options(updated) == ["all", "one", "new-stack"]
    assert _options(updated, sync.EXCLUDE_BEGIN, sync.EXCLUDE_END) == ["none", "one", "new-stack"]
    assert sync.render(updated, ["one", "new-stack"]) == updated
    inputs = yaml.safe_load(updated)[True]["workflow_dispatch"]["inputs"]
    assert inputs["exclude"]["description"] == "Skip one stack"
    assert inputs["exclude"]["type"] == "choice"
    assert inputs["exclude"]["default"] == "none"


@pytest.mark.parametrize("marker", [sync.EXCLUDE_BEGIN, sync.EXCLUDE_END])
def test_render_refuses_incomplete_exclusion_markers(marker):
    with pytest.raises(ValueError, match="generated exclude options"):
        sync.render(WORKFLOW + f"# {marker.removeprefix('# ')}\n", ["one"])


def _selection_manifest():
    return {"artifacts": {
        "acme-dev-network": {"type": "aws:cloudformation:stack", "properties": {}},
        "acme-dev-security": {
            "type": "aws:cloudformation:stack", "properties": {},
            "dependencies": ["acme-dev-network"],
        },
        "acme-dev-storage": {"type": "aws:cloudformation:stack", "properties": {}},
        "acme-dev-eks": {
            "type": "aws:cloudformation:stack", "properties": {},
            "dependencies": ["eks.assets"],
        },
        "eks.assets": {
            "type": "cdk:asset-manifest", "properties": {},
            "dependencies": ["acme-dev-security"],
        },
    }}


@pytest.mark.parametrize("excluded", ["eks", "acme-dev-eks", " eks, eks \n"])
def test_exclude_resolves_full_names_suffixes_and_duplicates(excluded):
    selected, omitted = selection.select_stacks(_selection_manifest(), "all", excluded)
    assert selected == ["acme-dev-network", "acme-dev-security", "acme-dev-storage"]
    assert omitted == ["acme-dev-eks"]


@pytest.mark.parametrize("requested, excluded, action, message", [
    ("all", "exk", "deploy", "unknown"),
    ("all", "--all", "deploy", "Invalid stack name"),
    ("all", "network security storage eks", "deploy", "no stacks"),
    ("acme-dev-eks", "storage", "deploy", "only be used with stacks=all"),
    ("all", "network", "deploy", "crosses the exclusion boundary"),
    ("all", "network,security", "diff", "crosses the exclusion boundary"),
    ("all", "eks", "destroy", "crosses the exclusion boundary"),
])
def test_invalid_or_dependency_crossing_exclusions_fail(requested, excluded, action, message):
    with pytest.raises(ValueError, match=message):
        selection.select_stacks(_selection_manifest(), requested, excluded, action)


def test_destroy_can_exclude_prerequisites_and_delete_their_dependents():
    selected, omitted = selection.select_stacks(
        _selection_manifest(), "all", "network,security,storage", "destroy",
    )
    assert selected == ["acme-dev-eks"]
    assert omitted == ["acme-dev-network", "acme-dev-security", "acme-dev-storage"]


def test_ambiguous_suffix_requires_a_full_name():
    manifest = _selection_manifest()
    manifest["artifacts"]["acme-qa-eks"] = {
        "type": "aws:cloudformation:stack", "properties": {},
    }
    with pytest.raises(ValueError, match="ambiguous"):
        selection.select_stacks(manifest, "all", "eks")


@pytest.mark.parametrize("requested, excluded, expected", [
    ("", "", "--all"),
    ("all", "none", "--all"),
    ("acme-dev-eks", "none", "acme-dev-eks"),
    ("acme-dev-eks", "", "acme-dev-eks"),
    ("all", "eks, storage", "acme-dev-network acme-dev-security --exclusively"),
    ("all", "acme-dev-eks", "acme-dev-network acme-dev-security acme-dev-storage --exclusively"),
])
def test_workflow_selection_exports_reviewed_targets(tmp_path, requested, excluded, expected):
    workflow = yaml.safe_load((WORKFLOWS / "deploy-primary-mdpp-dev.yml").read_text())
    step = next(step for step in workflow["jobs"]["preview"]["steps"] if step.get("id") == "select")
    assert step["env"] == {
        "REQUESTED": "${{ inputs.stacks }}", "EXCLUDED": "${{ inputs.exclude }}",
        "ACTION": "${{ inputs.action }}",
    }
    assert workflow[True]["workflow_dispatch"]["inputs"]["exclude"]["default"] == "none"
    outdir = tmp_path / "cdk.out"
    outdir.mkdir()
    (outdir / "manifest.json").write_text(json.dumps(_selection_manifest()))
    output = tmp_path / "output"
    environment_file = tmp_path / "environment"
    summary = tmp_path / "summary"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"].replace("scripts/select_stacks.py", str(SELECTION_SCRIPT))],
        cwd=tmp_path,
        env={
            **os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "REQUESTED": requested, "EXCLUDED": excluded, "ACTION": "deploy",
            "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(environment_file),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == f"targets={expected}\n"
    assert environment_file.read_text().splitlines()[0] == f"TARGETS={expected}"
    if excluded == "eks, storage":
        assert summary.read_text() == (
            "### Selected stacks\n\n- `acme-dev-network`\n- `acme-dev-security`\n"
            "\n### Excluded stacks\n\n- `acme-dev-eks`\n- `acme-dev-storage`\n"
        )
    elif excluded == "none":
        assert "Excluded stacks" not in summary.read_text()


def test_exclude_eks_from_real_dev_assembly_preserves_foundations(tmp_path):
    built = _built_stacks("md-preprod/dev", tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    selected, omitted = selection.select_stacks(manifest, "all", "eks", "deploy")
    eks = "md-preproduction-use1-dev-eks"
    assert omitted == [eks]
    assert set(selected) == built - {eks}
    assert len(selected) == 6
    assert list(tmp_path.glob("*.nested.template.json"))  # apps stay configured for later
    with pytest.raises(ValueError, match="crosses the exclusion boundary"):
        selection.select_stacks(manifest, "all", "network", "deploy")


def test_environment_comes_from_the_job_env():
    assert sync.environment_of(WORKFLOW) == "dev"


def test_environment_comes_from_workflow_env_with_anchor():
    workflow = """\
env:
  ENV: &deployment_environment prod
jobs:
  cdk:
    environment: *deployment_environment
"""
    assert sync.environment_of(workflow) == "prod"
    assert yaml.safe_load(workflow)["jobs"]["cdk"]["environment"] == "prod"


def test_dev_workflow_uses_central_deployment_settings():
    workflow = yaml.safe_load(
        (WORKFLOWS / "deploy-primary-mdpp-dev.yml").read_text()
    )
    settings = workflow["env"]
    # PyYAML's YAML 1.1 loader interprets the unquoted "on" key as True.
    paths = workflow[True]["push"]["paths"]
    assert settings["DEPLOYMENT_CONFIG_PATH"] == f"deployments/{settings['ENV']}/**"
    assert paths[0] == settings["DEPLOYMENT_CONFIG_PATH"]
    job = workflow["jobs"]["apply"]
    assert settings["ENV"] == "md-preprod/dev"
    assert settings["DEPLOYMENT_BASE_PATH"] in paths
    assert settings["DEPLOYMENT_BASE_PATH"] == "deployments/md-preprod/base.yaml"
    assert job["environment"] == settings["GITHUB_ENVIRONMENT"] == "dev"
    assert "ENV" not in job.get("env", {})
    steps = {step["name"]: step for step in job["steps"] if "name" in step}
    credentials = steps["Configure AWS credentials"]["with"]
    assert credentials["role-to-assume"] == "${{ env.AWS_ROLE_ARN }}"
    assert credentials["aws-region"] == "${{ env.AWS_REGION }}"
    preview = workflow["jobs"]["preview"]
    preview_steps = {step["name"]: step for step in preview["steps"] if "name" in step}
    assert preview_steps["Check destroy confirmation"]["if"] == (
        "inputs.action == 'destroy' && inputs.confirm != env.GITHUB_ENVIRONMENT"
    )
    assert "environment" not in preview
    assert preview_steps["Configure AWS credentials"]["with"]["role-to-assume"] == (
        "${{ env.AWS_PREVIEW_ROLE_ARN }}"
    )
    assert "--no-change-set" in preview_steps["Diff"]["run"]
    assert preview_steps["Read deployment settings"]["id"] == "deployment"
    assert job["env"]["AWS_REGION"] == "${{ needs.preview.outputs.aws_region }}"
    assert credentials["allowed-account-ids"] == "${{ env.AWS_ACCOUNT_ID }}"
    assert preview_steps["Configure AWS credentials"]["with"]["allowed-account-ids"] == (
        "${{ steps.deployment.outputs.account_id }}"
    )
    assert job["needs"] == "preview"
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && "
        "(inputs.action == 'deploy' || inputs.action == 'destroy')"
    )
    assert job["env"]["TARGETS"] == "${{ needs.preview.outputs.targets }}"
    assert "--app cdk.out" in steps["Deploy"]["run"]
    assert "--app cdk.out" in steps["Destroy"]["run"]
    assert steps["Download reviewed assembly"]["with"]["name"] == (
        preview_steps["Save reviewed assembly"]["with"]["name"]
    )


def test_workflow_reads_merged_deployment_settings(tmp_path):
    workflow = yaml.safe_load((WORKFLOWS / "deploy-primary-mdpp-dev.yml").read_text())
    step = next(
        step for step in workflow["jobs"]["preview"]["steps"]
        if step.get("id") == "deployment"
    )
    output = tmp_path / "output"
    environment_file = tmp_path / "environment"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=REPO_ROOT,
        env={
            **os.environ, **workflow["env"], **step["env"],
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(environment_file),
        },
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    deployment = load_environment(workflow["env"]["ENV"])
    assert output.read_text().splitlines() == [
        f"aws_region={deployment.region}", f"account_id={deployment.account_id}",
    ]
    assert environment_file.read_text() == f"AWS_REGION={deployment.region}\n"


def test_pr_workflow_discovers_legacy_and_grouped_deployments(tmp_path):
    workflow = yaml.safe_load((WORKFLOWS / "pr.yml").read_text())
    discover = workflow["jobs"]["discover"]
    step = next(step for step in discover["steps"] if step.get("id") == "discover")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=REPO_ROOT,
        env={
            **os.environ, **step["env"],
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text().removeprefix("environments=")) == list_environments()
    assert workflow["jobs"]["check"]["needs"] == "discover"
    assert workflow["jobs"]["check"]["strategy"]["matrix"]["environment"] == (
        "${{ fromJSON(needs.discover.outputs.environments) }}"
    )


def test_sync_uses_workflow_capable_token():
    workflow = yaml.safe_load((WORKFLOWS / "sync-stack-choices.yml").read_text())
    steps = workflow["jobs"]["sync"]["steps"]
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["token"] == "${{ secrets.WORKFLOW_SYNC_TOKEN }}"
    assert steps[0]["name"] == "Check sync token"
    assert workflow["permissions"]["contents"] == "read"


def test_oidc_setup_separates_preview_and_deployment_permissions(tmp_path):
    log = tmp_path / "aws-calls.jsonl"
    aws = tmp_path / "aws"
    aws.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['AWS_CALL_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if 'get-caller-identity' in sys.argv:\n"
        "    print('533267408704')\n"
    )
    aws.chmod(0o755)
    result = subprocess.run(
        [
            "bash", str(REPO_ROOT / "scripts/create_github_oidc_roles.sh"),
            "--yes", "--account-id", "533267408704", "--environment", "dev",
            "--repo", "KahBrightTech/cognitech-AWS-CDK-repo",
            "--owner-id", "202037050", "--repo-id", "1383489754",
            "--region", "us-east-1",
        ],
        env={
            **os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "AWS_CALL_LOG": str(log),
        },
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    trust = {}
    for call in calls:
        if "update-assume-role-policy" in call:
            role = call[call.index("--role-name") + 1]
            trust[role] = json.loads(call[call.index("--policy-document") + 1])
    for role, suffix in (
        ("github-oidc-cdk-preview-dev", "ref:refs/heads/main"),
        ("github-oidc-cdk-deploy-dev", "environment:dev"),
    ):
        subjects = trust[role]["Statement"][0]["Condition"]["StringEquals"][
            "token.actions.githubusercontent.com:sub"
        ]
        assert len(subjects) == 2
        assert all(subject.endswith(f":{suffix}") for subject in subjects)
    preview_policy = next(
        json.loads(call[call.index("--policy-document") + 1])
        for call in calls
        if "put-role-policy" in call and "github-oidc-cdk-preview-dev" in call
    )
    allow, deny = preview_policy["Statement"]
    assert allow["Effect"] == "Allow"
    assert allow["Resource"].endswith("cdk-hnb659fds-lookup-role-533267408704-us-east-1")
    assert deny == {
        "Effect": "Deny", "Action": "sts:AssumeRole", "NotResource": allow["Resource"],
    }
    assert any(
        "attach-role-policy" in call and "github-oidc-cdk-preview-dev" in call
        and "arn:aws:iam::aws:policy/ReadOnlyAccess" in call
        for call in calls
    )


def test_environment_is_none_when_not_declared():
    assert sync.environment_of("jobs:\n  cdk:\n    runs-on: ubuntu-latest\n") is None


@pytest.mark.parametrize("path", DEPLOY_WORKFLOWS, ids=lambda p: p.name)
def test_committed_dropdown_matches_the_app(path, tmp_path):
    text = path.read_text()
    if sync.BEGIN not in text:
        pytest.skip(f"{path.name} has no generated stack options")

    env_name = sync.environment_of(text)
    assert env_name is not None, f"{path.name} does not say which environment it deploys"

    options = _options(text)
    assert options[0] == sync.ALL, "the first choice must be 'all'"
    built = _built_stacks(env_name, tmp_path)
    assert set(options[1:]) == built, (
        f"{path.name} is out of date. Run: python scripts/sync_stack_choices.py"
    )
    if sync.EXCLUDE_BEGIN in text:
        excluded_options = _options(text, sync.EXCLUDE_BEGIN, sync.EXCLUDE_END)
        assert excluded_options[0] == sync.NONE
        assert set(excluded_options[1:]) == built, (
            f"{path.name} exclusion dropdown is out of date. Run: python scripts/sync_stack_choices.py"
        )
