"""Lambda functions, their layers/log groups/roles, and invoke permissions."""

from __future__ import annotations

import re
from typing import Mapping

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.lambdas import (
    CodeProps,
    FunctionProps,
    InvocationProps,
    LambdaProps,
    export_key,
)
from cognitech_cdk.constructs.lambdas import construct_id as logical_id
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.stacks.base import EnvironmentStack

FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
ROLE_NAME = re.compile(r"[\w+=,.@-]{1,64}", flags=re.ASCII)


class LambdaStack(EnvironmentStack):
    """`lambdas` and `lambda_invocations` from the environment YAML.

    Unlike the Terraform module, a created execution role only gets write
    access to the function's own log group (plus VPC networking when the
    function runs in the VPC). Anything else belongs on an `iam.roles` entry
    referenced with `role:`.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: LambdaProps,
        network: Network | None = None,
        security_groups: Mapping[str, ec2.ISecurityGroup] | None = None,
        roles: Mapping[str, iam.IRole] | None = None,
        buckets: Mapping[str, s3.IBucket] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        self.props = props
        self._network = network
        self._security_groups = security_groups or {}
        self._roles = roles or {}
        self._buckets = buckets or {}

        self.functions: dict[str, lambda_.IFunction] = {}
        self.function_roles: dict[str, iam.IRole] = {}
        self.layers: dict[str, lambda_.ILayerVersion] = {}
        self.log_groups: dict[str, logs.ILogGroup] = {}
        self.invocations: dict[str, lambda_.CfnPermission] = {}

        for function in props.functions:
            self._add_function(function)
        for invocation in props.invocations:
            self._add_invocation(invocation)

    # --- functions --------------------------------------------------------

    def _add_function(self, props: FunctionProps) -> None:
        label = f"Lambda '{props.name}'"
        cid = logical_id(props.name)
        name = self.resource_name(props.name)
        if not FUNCTION_NAME.fullmatch(name):
            raise ValueError(
                f"{label}: function name '{name}' must be at most 64 letters, digits, "
                "'-' or '_'."
            )
        removal = (
            cdk.RemovalPolicy.DESTROY if props.removal_policy == "destroy"
            else cdk.RemovalPolicy.RETAIN
        )

        log_group = logs.LogGroup(
            self, f"{cid}LogGroup", log_group_name=f"/aws/lambda/{name}", removal_policy=removal
        )
        cfn_log_group = log_group.node.default_child
        if not isinstance(cfn_log_group, logs.CfnLogGroup):
            raise TypeError(f"{label}: expected a CloudWatch Logs CfnLogGroup.")
        cfn_log_group.retention_in_days = props.log_retention_days
        cdk.Tags.of(log_group).add("Name", name)

        role = self._role(props, cid, name, log_group)

        layers = []
        if props.layer is not None:
            layer = lambda_.LayerVersion(
                self,
                f"{cid}Layer",
                code=self._code(props.layer.code, f"{label}.layer", f"{cid}Layer"),
                compatible_runtimes=[props.runtime],
                description=props.layer.description,
                layer_version_name=self.resource_name(props.name, "layer"),
                removal_policy=removal,
            )
            self.layers[props.name] = layer
            layers.append(layer)

        function = lambda_.Function(
            self,
            f"{cid}Function",
            function_name=name,
            description=props.description,
            runtime=props.runtime,
            handler=props.handler,
            code=self._code(props.code, f"{label}.code", f"{cid}Code"),
            timeout=cdk.Duration.seconds(props.timeout) if props.timeout else None,
            memory_size=props.memory_size,
            role=role,
            environment=dict(props.environment) or None,
            layers=layers or None,
            log_group=log_group,
            **self._vpc(props, label),
        )
        function.node.add_dependency(role)
        cdk.Tags.of(function).add("Name", name)
        for key, value in props.tags.items():
            cdk.Tags.of(function).add(key, value)

        self.functions[props.name] = function
        self.function_roles[props.name] = role
        self.log_groups[props.name] = log_group

        key = export_key(props.name)
        self.output(f"{cid}FunctionArn", function.function_arn, f"{key}-function-arn")
        self.output(f"{cid}FunctionName", function.function_name, f"{key}-function-name")
        self.output(f"{cid}FunctionRoleArn", role.role_arn, f"{key}-function-role-arn")
        if props.layer is not None:
            self.output(
                f"{cid}LayerArn", self.layers[props.name].layer_version_arn,
                f"{key}-layer-arn",
            )

    def _role(
        self, props: FunctionProps, cid: str, name: str, log_group: logs.LogGroup
    ) -> iam.IRole:
        label = f"Lambda '{props.name}'"
        if props.role is not None:
            source = schema.lookup(self._roles, props.role, f"{label}.role")
            # Immutable so CDK never edits a role owned by the IAM stack.
            return iam.Role.from_role_arn(
                self, f"{cid}ExecutionRole", source.role_arn, mutable=False
            )
        role_name = self.resource_name(props.name, "exec-role")
        if not ROLE_NAME.fullmatch(role_name):
            raise ValueError(
                f"{label}: role name '{role_name}' must use IAM name characters and be "
                "at most 64 characters; shorten the function name or set 'role'."
            )
        role = iam.Role(
            self,
            f"{cid}ExecutionRole",
            role_name=role_name,
            description=f"Execution role for Lambda {name}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={
                "logs": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                            resources=[log_group.log_group_arn],
                        )
                    ]
                )
            },
        )
        if props.vpc is not None:
            role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                )
            )
        cdk.Tags.of(role).add("Name", role_name)
        return role

    def _code(self, code: CodeProps, label: str, cid: str) -> lambda_.Code:
        if code.path is not None:
            return lambda_.Code.from_asset(str(code.path))
        if code.bucket is not None:
            bucket = schema.lookup(self._buckets, code.bucket, f"{label}.bucket")
        else:
            bucket = s3.Bucket.from_bucket_name(self, f"{cid}Bucket", code.bucket_name)
        return lambda_.Code.from_bucket(bucket, code.key, code.version)

    def _vpc(self, props: FunctionProps, label: str) -> dict:
        if props.vpc is None:
            return {}
        if self._network is None:
            raise ValueError(f"{label}.vpc: a network is required to place it in the VPC.")
        subnets = (
            self._network.public_subnets if props.vpc.subnets == "public"
            else self._network.private_subnets
        )
        if not subnets:
            raise ValueError(f"{label}.vpc: the network has no {props.vpc.subnets} subnets.")
        return {
            "vpc": self._network.vpc,
            "vpc_subnets": self._network.subnet_selection(props.vpc.subnets),
            "allow_public_subnet": props.vpc.subnets == "public",
            "security_groups": [
                schema.lookup(
                    self._security_groups, group, f"{label}.vpc.security_groups"
                )
                for group in props.vpc.security_groups
            ],
        }

    # --- permissions ------------------------------------------------------

    def _add_invocation(self, props: InvocationProps) -> None:
        if props.function is not None:
            target = schema.lookup(
                self.functions, props.function, f"Lambda invocation '{props.name}'.function"
            ).function_arn
        else:
            target = props.function_arn
        source_account = props.source_account
        if props.principal_is_service and props.source_arn is None and source_account is None:
            # A bare service principal would let that service invoke the function
            # on behalf of any account (confused deputy); pin it to this one.
            source_account = self.account
        self.invocations[props.name] = lambda_.CfnPermission(
            self,
            f"{logical_id(props.name)}Permission",
            action="lambda:InvokeFunction",
            function_name=target,
            principal=props.principal,
            source_arn=props.source_arn,
            source_account=source_account,
        )
