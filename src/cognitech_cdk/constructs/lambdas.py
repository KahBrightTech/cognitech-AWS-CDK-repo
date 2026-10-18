"""Lambda functions and invoke permissions, mirroring the Terraform `Lambdas/Template`
and `Lambda-invocations` modules.

    lambdas:
      - name: eks-node-tagger
        description: Tags newly started EKS nodes
        runtime: python3.12
        handler: index.handler
        timeout: 60
        code:
          path: lambda/eks-node-tagger     # repo-relative directory or .zip
          # or an object that already exists in S3, as Terraform uses:
          # bucket: artifacts              # a name from the buckets list
          # bucket_name: my-private-bucket # or an existing bucket's name
          # key: lambdas/eks-node-tagger.zip
        layer:                             # optional; Terraform's per-function layer
          description: Shared libraries
          key: layers/layers.zip           # same bucket as `code` unless given
        environment:
          VPC_NAME_ABR: dev
        role: lambda-exec                  # optional, from iam.roles
        vpc:                               # optional
          subnets: private
          security_groups: [lambda]
        log_retention_days: 14

    lambda_invocations:
      - name: s3-invoke
        function: eks-node-tagger          # or function_arn
        principal: s3.amazonaws.com
        source_arn: arn:aws:s3:::my-bucket

Function names are `{account_name}-{region_prefix}-{network}-{name}`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from aws_cdk import aws_lambda as lambda_

from cognitech_cdk.common import schema
from cognitech_cdk.common.documents import repository_path

LOG_RETENTION_DAYS = (
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731,
    1096, 1827, 2192, 2557, 2922, 3288, 3653,
)
REMOVAL_POLICIES = ("retain", "destroy")
SERVICE_PRINCIPAL = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.amazonaws\.com(?:\.cn)?")
ACCOUNT_ID = re.compile(r"[0-9]{12}")
IAM_PRINCIPAL_ARN = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:(?:root|role/.+|user/.+)")
LAMBDA_ARN = re.compile(
    r"arn:aws[a-z-]*:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+(?::[A-Za-z0-9_$-]+)?"
)
SOURCE_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{0,12}:.+")
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
ENV_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
RESERVED_ENV = {
    "_HANDLER", "_X_AMZN_TRACE_ID", "AWS_DEFAULT_REGION", "AWS_REGION",
    "AWS_EXECUTION_ENV", "AWS_LAMBDA_FUNCTION_NAME", "AWS_LAMBDA_FUNCTION_MEMORY_SIZE",
    "AWS_LAMBDA_FUNCTION_VERSION", "AWS_LAMBDA_INITIALIZATION_TYPE",
    "AWS_LAMBDA_LOG_GROUP_NAME", "AWS_LAMBDA_LOG_STREAM_NAME", "AWS_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_LAMBDA_RUNTIME_API", "LAMBDA_TASK_ROOT", "LAMBDA_RUNTIME_DIR",
}


def construct_id(name: str) -> str:
    """`eks_node-tagger` -> `EksNodeTagger`, for readable logical ids."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[-_]+", name) if part)


def export_key(name: str) -> str:
    """Export names allow letters, digits, ':' and '-', so `_` becomes `-`."""
    return name.replace("_", "-").lower()


def check_unique_ids(names: list[str], label: str) -> None:
    """Names that differ only by `-`/`_` or case would share logical ids/exports."""
    seen: dict[str, str] = {}
    for name in names:
        for key in (construct_id(name).casefold(), export_key(name)):
            if key in seen and seen[key] != name:
                raise ValueError(
                    f"{label}: '{name}' and '{seen[key]}' would produce the same "
                    "logical id or export name."
                )
            seen[key] = name


def runtime(value: Any, label: str) -> lambda_.Runtime:
    text = schema.required_string(value, label)
    for item in lambda_.Runtime.ALL:
        if item.name == text and text not in ("nodejs", "FROM_IMAGE"):
            return item
    raise ValueError(f"{label}: unknown Lambda runtime '{text}'.")


@dataclass(frozen=True)
class CodeProps:
    """Function or layer code: a repository path, or an existing S3 object."""

    path: Path | None = None
    bucket: str | None = None
    bucket_name: str | None = None
    key: str | None = None
    version: str | None = None

    @classmethod
    def from_dict(
        cls, raw: Any, label: str, *, base_dir: Path | None, inherit: CodeProps | None = None
    ) -> CodeProps:
        data = schema.mapping(raw, label, {"path", "bucket", "bucket_name", "key", "version"})
        if "path" in data:
            if set(data) != {"path"}:
                raise ValueError(f"{label}: 'path' cannot be combined with S3 fields.")
            raw_path = schema.required_string(data["path"], f"{label}.path")
            path = repository_path(raw_path, f"{label}.path", base_dir=base_dir)
            if path.is_dir():
                if not any(path.iterdir()):
                    raise ValueError(f"{label}.path: directory '{raw_path}' is empty.")
            elif path.is_file():
                if path.suffix.lower() not in (".zip", ".jar"):
                    raise ValueError(
                        f"{label}.path: '{raw_path}' must be a directory or a .zip/.jar file."
                    )
            else:
                raise ValueError(f"{label}.path: '{raw_path}' does not exist in the repository.")
            return cls(path=path)

        bucket = data.get("bucket")
        bucket_name = data.get("bucket_name")
        if bucket is not None and bucket_name is not None:
            raise ValueError(f"{label}: provide only one of bucket or bucket_name.")
        if bucket is None and bucket_name is None and inherit is not None:
            bucket, bucket_name = inherit.bucket, inherit.bucket_name
        if bucket is None and bucket_name is None:
            raise ValueError(
                f"{label}: provide 'path', or 'key' with 'bucket' or 'bucket_name'."
            )
        if bucket is not None:
            schema.name(bucket, f"{label}.bucket")
        if bucket_name is not None and (
            not isinstance(bucket_name, str) or not BUCKET_NAME.fullmatch(bucket_name)
        ):
            raise ValueError(f"{label}.bucket_name must be an S3 bucket name.")
        key = schema.required_string(data.get("key"), f"{label}.key")
        if key.startswith("/"):
            raise ValueError(f"{label}.key must not start with '/'.")
        return cls(
            bucket=bucket, bucket_name=bucket_name, key=key,
            version=schema.string(data.get("version"), f"{label}.version"),
        )

    @property
    def from_s3(self) -> bool:
        return self.path is None


@dataclass(frozen=True)
class LayerProps:
    code: CodeProps
    description: str | None = None

    @classmethod
    def from_dict(
        cls, raw: Any, label: str, *, base_dir: Path | None, function_code: CodeProps
    ) -> LayerProps:
        data = schema.mapping(
            raw, label, {"description", "path", "bucket", "bucket_name", "key", "version"}
        )
        description = schema.string(data.get("description"), f"{label}.description")
        code_raw = {key: value for key, value in data.items() if key != "description"}
        inherit = function_code if function_code.from_s3 else None
        return cls(
            code=CodeProps.from_dict(code_raw, label, base_dir=base_dir, inherit=inherit),
            description=description,
        )


@dataclass(frozen=True)
class VpcConfigProps:
    subnets: str
    security_groups: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> VpcConfigProps:
        data = schema.mapping(
            raw, label, {"subnets", "security_groups"}, required=("subnets", "security_groups")
        )
        subnets = data["subnets"]
        if subnets not in ("public", "private"):
            raise ValueError(f"{label}.subnets must be 'public' or 'private'.")
        groups = schema.strings(
            data.get("security_groups"), f"{label}.security_groups", allow_empty=False
        )
        for group in groups:
            schema.name(group, f"{label}.security_groups entry")
        if len(set(groups)) != len(groups):
            raise ValueError(f"{label}.security_groups: duplicate group.")
        return cls(subnets=subnets, security_groups=groups)


@dataclass(frozen=True)
class FunctionProps:
    name: str
    runtime: lambda_.Runtime
    handler: str
    code: CodeProps
    description: str | None = None
    timeout: int | None = None
    memory_size: int | None = None
    layer: LayerProps | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    role: str | None = None
    vpc: VpcConfigProps | None = None
    log_retention_days: int = 14
    removal_policy: str = "retain"
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> FunctionProps:
        label = "lambdas entry"
        data = schema.mapping(
            raw, label,
            {
                "name", "description", "runtime", "handler", "timeout", "memory_size",
                "code", "layer", "environment", "role", "vpc", "log_retention_days",
                "removal_policy", "tags",
            },
            required=("name", "runtime", "handler", "code"),
        )
        name = schema.name(data["name"], "lambdas name")
        label = f"Lambda '{name}'"
        description = schema.string(data.get("description"), f"{label}.description")
        if description is not None and len(description) > 256:
            raise ValueError(f"{label}.description must be at most 256 characters.")
        code = CodeProps.from_dict(data["code"], f"{label}.code", base_dir=base_dir)
        layer = (
            LayerProps.from_dict(
                data["layer"], f"{label}.layer", base_dir=base_dir, function_code=code
            )
            if data.get("layer") is not None else None
        )
        environment = schema.string_map(data.get("environment"), f"{label}.environment")
        for key in environment:
            if not ENV_KEY.fullmatch(key) or key in RESERVED_ENV:
                raise ValueError(
                    f"{label}.environment: '{key}' is not a valid or is a reserved variable name."
                )
        role = data.get("role")
        if role is not None:
            schema.name(role, f"{label}.role")
        retention = schema.integer(
            data.get("log_retention_days"), f"{label}.log_retention_days", default=14
        )
        if retention not in LOG_RETENTION_DAYS:
            raise ValueError(
                f"{label}.log_retention_days must be one of "
                f"{', '.join(map(str, LOG_RETENTION_DAYS))}."
            )
        removal = schema.string(
            data.get("removal_policy"), f"{label}.removal_policy", default="retain"
        )
        if removal not in REMOVAL_POLICIES:
            raise ValueError(f"{label}.removal_policy must be 'retain' or 'destroy'.")
        return cls(
            name=name,
            runtime=runtime(data["runtime"], f"{label}.runtime"),
            handler=schema.required_string(data["handler"], f"{label}.handler"),
            code=code,
            description=description,
            timeout=schema.integer(
                data.get("timeout"), f"{label}.timeout", minimum=1, maximum=900
            ),
            memory_size=schema.integer(
                data.get("memory_size"), f"{label}.memory_size", minimum=128, maximum=10240
            ),
            layer=layer,
            environment=environment,
            role=role,
            vpc=(
                VpcConfigProps.from_dict(data["vpc"], f"{label}.vpc")
                if data.get("vpc") is not None else None
            ),
            log_retention_days=retention,
            removal_policy=removal,
            tags=schema.string_map(data.get("tags"), f"{label}.tags"),
        )


@dataclass(frozen=True)
class InvocationProps:
    """One `AWS::Lambda::Permission`, as in the Terraform `Lambda-invocations` module."""

    name: str
    principal: str
    function: str | None = None
    function_arn: str | None = None
    source_arn: str | None = None
    source_account: str | None = None

    @property
    def principal_is_service(self) -> bool:
        return bool(SERVICE_PRINCIPAL.fullmatch(self.principal))

    @classmethod
    def from_dict(cls, raw: Any) -> InvocationProps:
        label = "lambda_invocations entry"
        if isinstance(raw, Mapping) and "source_key" in raw:
            raise ValueError(
                f"{label}: 'source_key' is not supported here because the rule lives in "
                "the events stack, which depends on this one. EventsStack grants "
                "EventBridge permission to invoke its Lambda targets automatically; "
                "use 'source_arn' for any other source."
            )
        data = schema.mapping(
            raw, label,
            {"name", "function", "function_arn", "principal", "source_arn", "source_account"},
            required=("name", "principal"),
        )
        name = schema.name(data["name"], "lambda_invocations name")
        label = f"Lambda invocation '{name}'"
        function = data.get("function")
        function_arn = data.get("function_arn")
        if (function is None) == (function_arn is None):
            raise ValueError(f"{label}: provide exactly one of function or function_arn.")
        if function is not None:
            schema.name(function, f"{label}.function")
        if function_arn is not None and (
            not isinstance(function_arn, str) or not LAMBDA_ARN.fullmatch(function_arn)
        ):
            raise ValueError(f"{label}.function_arn must be a Lambda function ARN.")
        principal = data["principal"]
        if not isinstance(principal, str) or not (
            SERVICE_PRINCIPAL.fullmatch(principal)
            or ACCOUNT_ID.fullmatch(principal)
            or IAM_PRINCIPAL_ARN.fullmatch(principal)
        ):
            raise ValueError(
                f"{label}.principal must be an AWS service principal, a 12-digit "
                "account id or an IAM ARN; public ('*') permissions are not allowed."
            )
        source_arn = data.get("source_arn")
        if source_arn is not None and (
            not isinstance(source_arn, str) or not SOURCE_ARN.fullmatch(source_arn)
        ):
            raise ValueError(f"{label}.source_arn must be an ARN.")
        source_account = data.get("source_account")
        if source_account is not None:
            source_account = str(source_account)
            if not ACCOUNT_ID.fullmatch(source_account):
                raise ValueError(f"{label}.source_account must be a 12-digit account id.")
        return cls(
            name=name, principal=principal, function=function, function_arn=function_arn,
            source_arn=source_arn, source_account=source_account,
        )


@dataclass(frozen=True)
class LambdaProps:
    functions: tuple[FunctionProps, ...] = ()
    invocations: tuple[InvocationProps, ...] = ()

    def __post_init__(self) -> None:
        check_unique_ids([item.name for item in self.functions], "lambdas")
        check_unique_ids([item.name for item in self.invocations], "lambda_invocations")
        names = {item.name: item for item in self.functions}
        for invocation in self.invocations:
            if invocation.function is not None:
                schema.lookup(
                    names, invocation.function, f"Lambda invocation '{invocation.name}'.function"
                )

    def __bool__(self) -> bool:
        return bool(self.functions or self.invocations)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> LambdaProps:
        """Parse `{"lambdas": [...], "lambda_invocations": [...]}`; `None` is empty."""
        if raw is None:
            return cls()
        data = schema.mapping(raw, "lambda config", {"lambdas", "lambda_invocations"})
        return cls(
            functions=schema.items(
                data.get("lambdas"), "lambdas",
                lambda item: FunctionProps.from_dict(item, base_dir=base_dir),
            ),
            invocations=schema.items(
                data.get("lambda_invocations"), "lambda_invocations", InvocationProps.from_dict
            ),
        )
