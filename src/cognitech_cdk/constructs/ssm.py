"""SSM parameters and documents (Terraform `SSM-Parameter-store` and `SSM-Documents`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import aws_cdk as cdk
import yaml
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from cognitech_cdk.common import documents, schema

PARAMETER_TYPES = ("String", "StringList")
TIERS = {
    "Standard": ssm.ParameterTier.STANDARD,
    "Advanced": ssm.ParameterTier.ADVANCED,
    "Intelligent-Tiering": ssm.ParameterTier.INTELLIGENT_TIERING,
}
DOCUMENT_TYPES = (
    "Command", "Policy", "Automation", "Session", "Package", "ApplicationConfiguration",
    "ApplicationConfigurationSchema", "DeploymentStrategy", "ChangeCalendar",
    "Automation.ChangeTemplate", "ProblemAnalysis", "ProblemAnalysisTemplate",
    "CloudFormation", "ConformancePackTemplate", "QuickSetup",
)
DOCUMENT_FORMATS = ("YAML", "JSON", "TEXT")


def _choice(value: Any, label: str, choices: tuple[str, ...] | Mapping[str, Any], default: str) -> str:
    if value is None:
        return default
    if value not in choices:
        raise ValueError(f"{label} must be one of: {', '.join(choices)}.")
    return value


@dataclass(frozen=True)
class ParameterProps:
    name: str
    description: str
    type: str = "String"
    value: str | None = None
    secret_key: str | None = None
    parameter_name: str | None = None
    tier: str = "Standard"

    @classmethod
    def from_dict(cls, raw: Any) -> "ParameterProps":
        raw = schema.mapping(raw, "ssm.parameters item", {
            "name", "description", "type", "value", "secret_key", "parameter_name",
            "tier", "overwrite",
        }, required=("name",))
        name = schema.name(raw["name"], "ssm.parameters item name")
        label = f"ssm.parameters '{name}'"
        if raw.get("type") == "SecureString":
            raise ValueError(
                f"{label}: SecureString cannot be created by CloudFormation and its value would be "
                "stored in the template. Store the value in `secrets` and reference it with secret_key."
            )
        parameter_type = _choice(raw.get("type"), f"{label}.type", PARAMETER_TYPES, "String")
        if schema.boolean(raw.get("overwrite"), f"{label}.overwrite"):
            raise ValueError(f"{label}: overwrite is not supported; CloudFormation must own the parameter.")
        value = schema.string(raw.get("value"), f"{label}.value")
        secret_key = schema.string(raw.get("secret_key"), f"{label}.secret_key")
        if (value is None) == (secret_key is None):
            raise ValueError(f"{label}: set exactly one of value or secret_key.")
        if secret_key is not None and parameter_type != "String":
            raise ValueError(f"{label}: secret_key parameters must use type String.")
        parameter_name = schema.string(raw.get("parameter_name"), f"{label}.parameter_name")
        if parameter_name is not None and parameter_name.lower().lstrip("/").startswith(("aws", "ssm")):
            raise ValueError(f"{label}.parameter_name must not start with 'aws' or 'ssm'.")
        return cls(
            name=name,
            description=schema.required_string(raw.get("description"), f"{label}.description"),
            type=parameter_type,
            value=value,
            secret_key=secret_key,
            parameter_name=parameter_name,
            tier=_choice(raw.get("tier"), f"{label}.tier", TIERS, "Standard"),
        )


@dataclass(frozen=True)
class OutputLocationProps:
    s3_key_prefix: str | None = None
    s3_bucket_name: str | None = None
    bucket: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "OutputLocationProps":
        raw = schema.mapping(raw, label, {"s3_bucket_name", "s3_key_prefix", "bucket"})
        bucket_name = schema.string(raw.get("s3_bucket_name"), f"{label}.s3_bucket_name")
        bucket = schema.string(raw.get("bucket"), f"{label}.bucket")
        if (bucket_name is None) == (bucket is None):
            raise ValueError(f"{label}: set exactly one of s3_bucket_name or bucket.")
        return cls(
            s3_key_prefix=schema.string(raw.get("s3_key_prefix"), f"{label}.s3_key_prefix"),
            s3_bucket_name=bucket_name,
            bucket=bucket,
        )


@dataclass(frozen=True)
class DocumentProps:
    name: str
    content: Any
    document_type: str = "Command"
    document_format: str = "YAML"
    tags: Mapping[str, str] = field(default_factory=dict)
    create_association: bool = False
    target_key: str | None = None
    target_values: tuple[str, ...] = ()
    schedule_expression: str | None = None
    output_location: OutputLocationProps | None = None

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> "DocumentProps":
        raw = schema.mapping(raw, "ssm.documents item", {
            "name", "content", "content_file", "create_association", "document_type",
            "document_format", "tags", "targets", "schedule_expression", "output_location",
        }, required=("name",))
        name = schema.name(raw["name"], "ssm.documents item name")
        label = f"ssm.documents '{name}'"
        if name.lower().startswith(("aws", "amazon")):
            raise ValueError(f"{label}: document names must not start with aws or amazon.")
        document_format = _choice(raw.get("document_format"), f"{label}.document_format", DOCUMENT_FORMATS, "YAML")
        if ("content" in raw) == ("content_file" in raw):
            raise ValueError(f"{label}: set exactly one of content or content_file.")
        if "content_file" in raw:
            text = documents.load_text(
                schema.required_string(raw["content_file"], f"{label}.content_file"),
                f"{label}.content_file", base_dir=base_dir,
            )
        else:
            text = raw["content"]
        content = _content(text, document_format, label)

        create_association = schema.boolean(raw.get("create_association"), f"{label}.create_association")
        association_fields = [key for key in ("targets", "schedule_expression", "output_location") if raw.get(key) is not None]
        if association_fields and not create_association:
            raise ValueError(f"{label}: {', '.join(association_fields)} require create_association: true.")
        target_key, target_values = None, ()
        if raw.get("targets") is not None:
            targets = schema.mapping(raw["targets"], f"{label}.targets", {"key", "values"}, required=("key", "values"))
            target_key = schema.required_string(targets["key"], f"{label}.targets.key")
            target_values = schema.strings(targets["values"], f"{label}.targets.values", allow_empty=False)
        output_location = None
        if raw.get("output_location") is not None:
            output_location = OutputLocationProps.from_dict(raw["output_location"], f"{label}.output_location")
        return cls(
            name=name,
            content=content,
            document_type=_choice(raw.get("document_type"), f"{label}.document_type", DOCUMENT_TYPES, "Command"),
            document_format=document_format,
            tags=schema.string_map(raw.get("tags"), f"{label}.tags"),
            create_association=create_association,
            target_key=target_key,
            target_values=target_values,
            schedule_expression=schema.string(raw.get("schedule_expression"), f"{label}.schedule_expression"),
            output_location=output_location,
        )


def _content(raw: Any, document_format: str, label: str) -> Any:
    """CloudFormation takes JSON/YAML content as an object and TEXT as a string."""
    if document_format == "TEXT":
        return schema.required_string(raw, f"{label}.content")
    if isinstance(raw, str):
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise ValueError(f"{label}.content: invalid {document_format}: {error}") from error
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(f"{label}.content must be a nonempty {document_format} document.")
    return documents.json_compatible(raw, f"{label}.content")


@dataclass(frozen=True)
class SsmProps:
    parameters: tuple[ParameterProps, ...] = ()
    documents: tuple[DocumentProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.parameters or self.documents)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> "SsmProps":
        if raw is None:
            return cls()
        raw = schema.mapping(raw, "ssm", {"parameters", "documents"})
        return cls(
            parameters=schema.items(raw.get("parameters"), "ssm.parameters", ParameterProps.from_dict),
            documents=schema.items(
                raw.get("documents"), "ssm.documents",
                lambda item: DocumentProps.from_dict(item, base_dir=base_dir),
            ),
        )


class Ssm(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        props: SsmProps,
        naming: Callable[..., str],
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
        buckets: Mapping[str, s3.IBucket] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        secrets = secrets or {}
        buckets = buckets or {}
        self.parameters: dict[str, ssm.IParameter] = {}
        self.documents: dict[str, ssm.CfnDocument] = {}
        self.associations: dict[str, ssm.CfnAssociation] = {}

        for item in props.parameters:
            label = f"ssm.parameters '{item.name}'"
            parameter_name = item.parameter_name or naming(item.name)
            if item.secret_key is not None:
                value = schema.lookup(secrets, item.secret_key, f"{label}.secret_key").secret_name
            else:
                value = item.value
            if item.type == "StringList":
                parameter: ssm.IParameter = ssm.StringListParameter(
                    self, item.name, parameter_name=parameter_name, description=item.description,
                    string_list_value=value.split(","), tier=TIERS[item.tier],
                )
            else:
                parameter = ssm.StringParameter(
                    self, item.name, parameter_name=parameter_name, description=item.description,
                    string_value=value, tier=TIERS[item.tier],
                )
            cdk.Tags.of(parameter).add("Name", parameter_name)
            self.parameters[item.name] = parameter

        for item in props.documents:
            label = f"ssm.documents '{item.name}'"
            document_name = naming(item.name)
            document = ssm.CfnDocument(
                self,
                item.name,
                name=document_name,
                content=item.content,
                document_type=item.document_type,
                document_format=item.document_format,
                update_method="NewVersion",
                tags=[
                    cdk.CfnTag(key=key, value=value)
                    for key, value in {**item.tags, "Name": document_name}.items()
                ],
            )
            self.documents[item.name] = document
            if not item.create_association:
                continue
            output_location = None
            if item.output_location is not None:
                location = item.output_location
                bucket_name = location.s3_bucket_name or schema.lookup(
                    buckets, location.bucket, f"{label}.output_location.bucket"
                ).bucket_name
                output_location = ssm.CfnAssociation.InstanceAssociationOutputLocationProperty(
                    s3_location=ssm.CfnAssociation.S3OutputLocationProperty(
                        output_s3_bucket_name=bucket_name,
                        output_s3_key_prefix=location.s3_key_prefix,
                    )
                )
            self.associations[item.name] = ssm.CfnAssociation(
                self,
                f"{item.name}Association",
                name=document.ref,
                targets=[ssm.CfnAssociation.TargetProperty(key=item.target_key, values=list(item.target_values))]
                if item.target_key else None,
                schedule_expression=item.schedule_expression,
                output_location=output_location,
            )
