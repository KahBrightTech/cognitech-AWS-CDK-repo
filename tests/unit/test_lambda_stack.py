"""Lambda functions, layers, roles, VPC placement, invoke permissions and outputs."""

from __future__ import annotations

import json
import textwrap

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.lambdas import LambdaProps
from cognitech_cdk.stacks.iam_stack import IamStack
from cognitech_cdk.stacks.lambda_stack import LambdaStack
from cognitech_cdk.stacks.network_stack import NetworkStack
from cognitech_cdk.stacks.security_stack import SecurityStack

NETWORK = "name: uat\ncidr_block: 10.20.0.0/16\nazs: 2\nprivate_subnets: true"
PREFIX = "int-production-use1-uat"

S3_FUNCTION = """
lambdas:
- name: eks_node_tagger
  description: Lambda function to tag EKS nodes
  runtime: python3.12
  handler: index.lambda_handler
  timeout: 60
  code:
    bucket_name: int-production-private-bucket
    key: lambdas/eks-node-tagger/index.zip
  layer:
    description: Shared libraries
    key: lambdas/layers/layers.zip
  environment:
    VPC_NAME_ABR: uat
  tags:
    Used_for: eks-node-tagging
"""


def _props(text: str, base_dir=None) -> LambdaProps:
    return LambdaProps.from_dict(yaml.safe_load(textwrap.dedent(text)), base_dir=base_dir)


def _environment(write_env, **kwargs):
    return load_environment("test", root=write_env(NETWORK, **kwargs))


def _stack(write_env, text=S3_FUNCTION, *, base_dir=None, **dependencies):
    environment = _environment(write_env)
    app = cdk.App()
    stack = LambdaStack(
        app,
        "lambda",
        environment=environment,
        props=_props(text, base_dir),
        env=cdk.Environment(account=environment.account_id, region=environment.region),
        **dependencies,
    )
    return stack, Template.from_stack(stack)


# --- what gets built ------------------------------------------------------


def test_function_mirrors_the_terraform_template(write_env):
    _, template = _stack(write_env)

    template.resource_count_is("AWS::Lambda::Function", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": f"{PREFIX}-eks_node_tagger",
            "Description": "Lambda function to tag EKS nodes",
            "Runtime": "python3.12",
            "Handler": "index.lambda_handler",
            "Timeout": 60,
            "Code": {
                "S3Bucket": "int-production-private-bucket",
                "S3Key": "lambdas/eks-node-tagger/index.zip",
            },
            "Environment": {"Variables": {"VPC_NAME_ABR": "uat"}},
            "Layers": [{"Ref": Match.string_like_regexp("EksNodeTaggerLayer")}],
            "LoggingConfig": {"LogGroup": {"Ref": Match.string_like_regexp("LogGroup")}},
        },
    )
    function = next(iter(template.find_resources("AWS::Lambda::Function").values()))
    tags = {tag["Key"]: tag["Value"] for tag in function["Properties"]["Tags"]}
    assert tags["Name"] == f"{PREFIX}-eks_node_tagger"
    assert tags["Used_for"] == "eks-node-tagging"
    assert tags["Compliance"] == "hippaa"


def test_layer_uses_the_function_bucket_and_runtime(write_env):
    _, template = _stack(write_env)

    template.has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {
            "LayerName": f"{PREFIX}-eks_node_tagger-layer",
            "Description": "Shared libraries",
            "CompatibleRuntimes": ["python3.12"],
            "Content": {
                "S3Bucket": "int-production-private-bucket",
                "S3Key": "lambdas/layers/layers.zip",
            },
        },
    )
    template.has_resource("AWS::Lambda::LayerVersion", {"DeletionPolicy": "Retain"})


def test_log_group_is_named_for_the_function_and_retained(write_env):
    _, template = _stack(write_env)

    template.has_resource(
        "AWS::Logs::LogGroup",
        {
            "Properties": {
                "LogGroupName": f"/aws/lambda/{PREFIX}-eks_node_tagger",
                "RetentionInDays": 14,
            },
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
        },
    )


def test_created_role_only_writes_its_own_logs(write_env):
    """Terraform granted `*` on `*`; this role gets the log group and nothing else."""
    _, template = _stack(write_env)

    roles = template.find_resources("AWS::IAM::Role")
    assert len(roles) == 1
    role = next(iter(roles.values()))["Properties"]
    assert role["RoleName"] == f"{PREFIX}-eks_node_tagger-exec-role"
    assert role["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
        }
    ]
    assert "ManagedPolicyArns" not in role
    log_group = next(iter(template.find_resources("AWS::Logs::LogGroup")))
    assert role["Policies"][0]["PolicyDocument"]["Statement"] == [
        {
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Effect": "Allow",
            "Resource": {"Fn::GetAtt": [log_group, "Arn"]},
        }
    ]
    template.resource_count_is("AWS::IAM::Policy", 0)
    assert '"*"' not in json.dumps(role["Policies"])


def test_code_can_come_from_a_repository_path(write_env, tmp_path):
    (tmp_path / "functions" / "tagger").mkdir(parents=True)
    (tmp_path / "functions" / "tagger" / "index.py").write_text(
        "def handler(event, context):\n    return event\n", encoding="utf-8"
    )
    _, template = _stack(
        write_env,
        """
        lambdas:
        - name: tagger
          runtime: python3.13
          handler: index.handler
          code:
            path: functions/tagger
        """,
        base_dir=tmp_path,
    )

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Code": {
                "S3Bucket": Match.string_like_regexp("^cdk-.*-assets-"),
                "S3Key": Match.string_like_regexp(r"\.zip$"),
            }
        },
    )


def test_code_can_come_from_a_configured_bucket(write_env):
    environment = _environment(write_env)
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    storage = cdk.Stack(app, "storage", env=env)
    bucket = s3.Bucket(storage, "Artifacts")
    stack = LambdaStack(
        app, "lambda", environment=environment, env=env,
        props=_props(
            """
            lambdas:
            - name: tagger
              runtime: python3.12
              handler: index.handler
              code:
                bucket: artifacts
                key: tagger.zip
            """
        ),
        buckets={"artifacts": bucket},
    )

    function = next(iter(Template.from_stack(stack).find_resources(
        "AWS::Lambda::Function"
    ).values()))
    assert "Fn::ImportValue" in function["Properties"]["Code"]["S3Bucket"]
    assert storage in stack.dependencies


def test_a_referenced_role_is_used_without_changing_the_iam_stack(write_env):
    environment = _environment(
        write_env,
        iam="""
        roles:
        - name: lambda-exec
          assumed_by: lambda.amazonaws.com
          managed_policies: [service-role/AWSLambdaVPCAccessExecutionRole]
        """,
    )
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    before = Template.from_stack(
        IamStack(cdk.App(), "iam", environment=environment, env=env)
    ).to_json()
    app = cdk.App()
    iam_stack = IamStack(app, "iam", environment=environment, env=env)
    text = S3_FUNCTION.replace("  timeout: 60\n", "  timeout: 60\n  role: lambda-exec\n")
    stack = LambdaStack(
        app, "lambda", environment=environment, env=env, props=_props(text),
        roles=iam_stack.iam.roles,
    )
    template = Template.from_stack(stack)

    template.resource_count_is("AWS::IAM::Role", 0)
    template.resource_count_is("AWS::IAM::Policy", 0)
    function = next(iter(template.find_resources("AWS::Lambda::Function").values()))
    assert "Fn::ImportValue" in json.dumps(function["Properties"]["Role"])
    after = Template.from_stack(iam_stack).to_json()
    # CDK adds an export for the cross-stack reference; IAM resources and existing outputs stay put.
    assert after["Resources"] == before["Resources"]
    assert before["Outputs"].items() <= after["Outputs"].items()


def test_vpc_functions_use_the_network_and_security_groups(write_env):
    environment = _environment(
        write_env,
        security_groups="""
        - name: lambda
          description: Lambda functions
        """,
    )
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network = NetworkStack(app, "network", environment=environment, env=env)
    security = SecurityStack(
        app, "security", environment=environment, vpc=network.network.vpc, env=env
    )
    text = S3_FUNCTION + "  vpc:\n    subnets: private\n    security_groups: [lambda]\n"
    stack = LambdaStack(
        app, "lambda", environment=environment, env=env, props=_props(text),
        network=network.network, security_groups=security.security_groups.groups,
    )
    template = Template.from_stack(stack)

    function = next(iter(template.find_resources("AWS::Lambda::Function").values()))
    vpc_config = function["Properties"]["VpcConfig"]
    assert len(vpc_config["SubnetIds"]) == 2
    assert all("Fn::ImportValue" in subnet for subnet in vpc_config["SubnetIds"])
    assert "Fn::ImportValue" in vpc_config["SecurityGroupIds"][0]
    template.resource_count_is("AWS::EC2::SecurityGroup", 0)
    template.has_resource_properties(
        "AWS::IAM::Role",
        {"ManagedPolicyArns": [{"Fn::Join": ["", Match.array_with([
            ":iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
        ])]}]},
    )
    assert {network, security} <= set(stack.dependencies)


def test_vpc_placement_needs_a_network(write_env):
    text = S3_FUNCTION + "  vpc:\n    subnets: private\n    security_groups: [lambda]\n"
    with pytest.raises(ValueError, match="a network is required"):
        _stack(write_env, text)


def test_invocation_permissions_mirror_lambda_invocations(write_env):
    stack, template = _stack(
        write_env,
        S3_FUNCTION + """
lambda_invocations:
- name: s3-invoke
  function: eks_node_tagger
  principal: s3.amazonaws.com
  source_arn: arn:aws:s3:::int-production-private-bucket
  source_account: "111122223333"
- name: other-account
  function_arn: arn:aws:lambda:us-east-1:444455556666:function:remote
  principal: "444455556666"
- name: service-without-source
  function: eks_node_tagger
  principal: sns.amazonaws.com
""",
    )

    assert set(stack.invocations) == {"s3-invoke", "other-account", "service-without-source"}
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunction",
            "FunctionName": {"Fn::GetAtt": [Match.string_like_regexp("Function"), "Arn"]},
            "Principal": "s3.amazonaws.com",
            "SourceArn": "arn:aws:s3:::int-production-private-bucket",
            "SourceAccount": "111122223333",
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "FunctionName": "arn:aws:lambda:us-east-1:444455556666:function:remote",
            "Principal": "444455556666",
        },
    )
    # A bare service principal is pinned to this account (confused deputy).
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Principal": "sns.amazonaws.com", "SourceAccount": "111122223333"},
    )


def test_outputs_have_stable_export_names(write_env):
    _, template = _stack(write_env)

    exports = {
        output["Export"]["Name"] for output in template.find_outputs("*").values()
    }
    assert exports == {
        "lambda-eks-node-tagger-function-arn",
        "lambda-eks-node-tagger-function-name",
        "lambda-eks-node-tagger-function-role-arn",
        "lambda-eks-node-tagger-layer-arn",
    }


def test_public_attributes_are_keyed_by_config_name(write_env):
    stack, _ = _stack(write_env)

    assert set(stack.functions) == {"eks_node_tagger"}
    assert set(stack.layers) == {"eks_node_tagger"}
    assert stack.invocations == {}


# --- what the config refuses ---------------------------------------------


def test_empty_config_creates_nothing():
    assert not LambdaProps.from_dict(None)
    assert not LambdaProps.from_dict({"lambdas": [], "lambda_invocations": None})
    assert _props(S3_FUNCTION)


@pytest.mark.parametrize(
    "text, message",
    [
        (S3_FUNCTION.replace("  timeout: 60", "  timeout: 60\n  memory: 1"), "unknown fields: memory"),
        (S3_FUNCTION.replace("python3.12", "python9"), "unknown Lambda runtime"),
        (S3_FUNCTION.replace("timeout: 60", "timeout: 901"), "at most 900"),
        (
            S3_FUNCTION.replace("    bucket_name: int-production-private-bucket\n", ""),
            "provide 'path', or 'key'",
        ),
        (S3_FUNCTION + "- name: EKS_NODE_TAGGER\n  runtime: python3.12\n  handler: x\n"
         "  code: {bucket_name: abc, key: k}\n", "duplicate name"),
        (S3_FUNCTION + "- name: eks-node-tagger\n  runtime: python3.12\n  handler: x\n"
         "  code: {bucket_name: abc, key: k}\n", "same logical id"),
        (S3_FUNCTION.replace("VPC_NAME_ABR", "AWS_REGION"), "reserved"),
        (S3_FUNCTION + "  log_retention_days: 15\n", "log_retention_days must be one of"),
        (S3_FUNCTION + "  vpc:\n    subnets: isolated\n    security_groups: [a]\n", "'public' or 'private'"),
        (S3_FUNCTION + "  vpc:\n    subnets: private\n", "missing required fields: security_groups"),
    ],
)
def test_bad_function_config_is_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        _props(text)


@pytest.mark.parametrize("path, message", [
    ("missing", "does not exist"),
    ("../outside", "remain under the repository"),
    ("/abs/path", "local relative path"),
    ("handler.py", "directory or a .zip"),
    ("empty", "is empty"),
])
def test_code_paths_must_exist_inside_the_repository(tmp_path, path, message):
    (tmp_path / "empty").mkdir()
    (tmp_path / "handler.py").write_text("x = 1\n", encoding="utf-8")
    text = f"""
    lambdas:
    - name: fn
      runtime: python3.12
      handler: index.handler
      code:
        path: {path}
    """
    with pytest.raises(ValueError, match=message):
        _props(text, tmp_path)


def test_a_path_cannot_be_mixed_with_s3_fields(tmp_path):
    (tmp_path / "fn.zip").write_bytes(b"PK")
    with pytest.raises(ValueError, match="cannot be combined"):
        _props(
            """
            lambdas:
            - name: fn
              runtime: python3.12
              handler: index.handler
              code: {path: fn.zip, key: x}
            """,
            tmp_path,
        )


@pytest.mark.parametrize(
    "invocation, message",
    [
        ("{name: x, function: nope, principal: s3.amazonaws.com}", "unknown reference 'nope'"),
        ("{name: x, function: eks_node_tagger, principal: '*'}", "public"),
        ("{name: x, function: eks_node_tagger, principal: s3.amazonaws.com, source_key: rule}",
         "EventsStack grants"),
        ("{name: x, principal: s3.amazonaws.com}", "exactly one of function or function_arn"),
        ("{name: x, function: eks_node_tagger, principal: s3.amazonaws.com, statement_id: A}",
         "unknown fields: statement_id"),
        ("{name: x, function_arn: 'not-an-arn', principal: s3.amazonaws.com}",
         "Lambda function ARN"),
        ("{name: x, function: eks_node_tagger, principal: s3.amazonaws.com, source_account: 12}",
         "12-digit"),
    ],
)
def test_bad_invocations_are_rejected(invocation, message):
    with pytest.raises(ValueError, match=message):
        _props(S3_FUNCTION + f"lambda_invocations:\n- {invocation}\n")


def test_unknown_references_fail_at_synth(write_env):
    with pytest.raises(ValueError, match="unknown reference 'artifacts'"):
        _stack(
            write_env,
            """
            lambdas:
            - name: fn
              runtime: python3.12
              handler: index.handler
              code: {bucket: artifacts, key: fn.zip}
            """,
        )
    with pytest.raises(ValueError, match="unknown reference 'lambda-exec'"):
        _stack(write_env, S3_FUNCTION + "  role: lambda-exec\n")


def test_overlong_function_names_fail(write_env):
    with pytest.raises(ValueError, match="at most 64"):
        _stack(write_env, S3_FUNCTION.replace("eks_node_tagger", "x" * 50))
