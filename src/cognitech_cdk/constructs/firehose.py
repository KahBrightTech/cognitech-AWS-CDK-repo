"""Kinesis Data Firehose delivery streams, mirroring the Terraform `AWS-Firehose` module.

    firehose_streams:
      - name: eks-logs
        destination: opensearch            # extended_s3 (default) | opensearch
        role: firehose                     # iam.roles name or role_arn; created if omitted
        s3_configuration:                  # destination, or backup for opensearch
          bucket: audit-bucket             # s3_buckets name, or bucket_arn
          prefix: firehose/
        opensearch_configuration:
          domain: logs                     # opensearch_domains name, or domain_arn
          index_name: eks
          vpc_config: {subnets: private, security_groups: [firehose]}

When no role is given, the stack creates one limited to the configured bucket,
domain, log group and KMS key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cognitech_cdk.common import schema

DESTINATIONS = ("extended_s3", "opensearch")
COMPRESSION = ("UNCOMPRESSED", "GZIP", "ZIP", "Snappy", "HADOOP_SNAPPY")
ROTATION = ("NoRotation", "OneHour", "OneDay", "OneWeek", "OneMonth")
BACKUP_MODES = ("FailedDocumentsOnly", "AllDocuments")
KEY_TYPES = ("AWS_OWNED_CMK", "CUSTOMER_MANAGED_CMK")
PROCESSOR_TYPES = ("RecordDeAggregation", "Decompression", "CloudWatchLogProcessing", "Lambda",
                   "MetadataExtraction", "AppendDelimiterToRecord")
LOG_RETENTION_DAYS = (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827,
                      2192, 2557, 2922, 3288, 3653)
ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{0,12}:.+")
STREAM_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _choice(value: Any, label: str, choices: tuple[str, ...], default: str) -> str:
    result = schema.string(value, label, default=default)
    if result not in choices:
        raise ValueError(f"{label} must be one of {', '.join(choices)}.")
    return result


def _arn(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not ARN.fullmatch(value):
        raise ValueError(f"{label} must be an ARN.")
    return value


def _reference(data: Any, label: str, key: str) -> tuple[str | None, str | None]:
    name = data.get(key)
    arn = data.get(f"{key}_arn")
    if (name is None) == (arn is None):
        raise ValueError(f"{label}: provide exactly one of {key} or {key}_arn.")
    return (schema.name(name, f"{label}: {key}") if name is not None else None,
            _arn(arn, f"{label}: {key}_arn"))


def _buffer(data: Any, label: str, *, size: int = 5, interval: int = 300) -> tuple[int, int]:
    return (
        schema.integer(data.get("buffering_size"), f"{label}: buffering_size", default=size, minimum=1, maximum=128) or size,
        schema.integer(data.get("buffering_interval"), f"{label}: buffering_interval", default=interval,
                       minimum=0, maximum=900) or 0,
    )


@dataclass(frozen=True)
class S3Props:
    bucket: str | None
    bucket_arn: str | None
    prefix: str = ""
    error_output_prefix: str = "errors/"
    buffering_size: int = 5
    buffering_interval: int = 300
    compression_format: str = "GZIP"

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> S3Props:
        data = schema.mapping(raw, label, {
            "bucket", "bucket_arn", "prefix", "error_output_prefix", "buffering_size",
            "buffering_interval", "compression_format",
        })
        bucket, bucket_arn = _reference(data, label, "bucket")
        size, interval = _buffer(data, label)
        return cls(
            bucket=bucket,
            bucket_arn=bucket_arn,
            prefix=schema.string(data.get("prefix"), f"{label}: prefix", default="", allow_empty=True) or "",
            error_output_prefix=schema.string(
                data.get("error_output_prefix"), f"{label}: error_output_prefix", default="errors/"
            ) or "errors/",
            buffering_size=size,
            buffering_interval=interval,
            compression_format=_choice(data.get("compression_format"), f"{label}: compression_format",
                                       COMPRESSION, "GZIP"),
        )


@dataclass(frozen=True)
class OpenSearchDestinationProps:
    domain: str | None
    domain_arn: str | None
    index_name: str
    index_rotation_period: str = "OneDay"
    type_name: str | None = None
    buffering_size: int = 5
    buffering_interval: int = 300
    retry_duration: int = 300
    s3_backup_mode: str = "FailedDocumentsOnly"
    subnets: str | None = None
    security_groups: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> OpenSearchDestinationProps:
        data = schema.mapping(raw, label, {
            "domain", "domain_arn", "index_name", "index_rotation_period", "type_name",
            "buffering_size", "buffering_interval", "retry_duration", "s3_backup_mode", "vpc_config",
        }, required=("index_name",))
        domain, domain_arn = _reference(data, label, "domain")
        size, interval = _buffer(data, label)
        subnets = None
        groups: tuple[str, ...] = ()
        if data.get("vpc_config") is not None:
            vpc = schema.mapping(data["vpc_config"], f"{label}: vpc_config", {"subnets", "security_groups"},
                                 required=("security_groups",))
            subnets = _choice(vpc.get("subnets"), f"{label}: vpc_config.subnets", ("private", "public"), "private")
            groups = schema.strings(vpc["security_groups"], f"{label}: vpc_config.security_groups",
                                    allow_empty=False)
        return cls(
            domain=domain,
            domain_arn=domain_arn,
            index_name=schema.required_string(data["index_name"], f"{label}: index_name"),
            index_rotation_period=_choice(data.get("index_rotation_period"), f"{label}: index_rotation_period",
                                          ROTATION, "OneDay"),
            type_name=schema.string(data.get("type_name"), f"{label}: type_name"),
            buffering_size=size,
            buffering_interval=interval,
            retry_duration=schema.integer(data.get("retry_duration"), f"{label}: retry_duration",
                                          default=300, minimum=0, maximum=7200) or 0,
            s3_backup_mode=_choice(data.get("s3_backup_mode"), f"{label}: s3_backup_mode",
                                   BACKUP_MODES, "FailedDocumentsOnly"),
            subnets=subnets,
            security_groups=groups,
        )


@dataclass(frozen=True)
class ProcessorProps:
    type: str
    parameters: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FirehoseStreamProps:
    name: str
    destination: str = "extended_s3"
    role: str | None = None
    role_arn: str | None = None
    enable_cloudwatch_logging: bool = True
    log_retention_days: int = 14
    s3: S3Props | None = None
    opensearch: OpenSearchDestinationProps | None = None
    processing_enabled: bool = True
    processors: tuple[ProcessorProps, ...] = ()
    kinesis_stream_arn: str | None = None
    kinesis_role_arn: str | None = None
    encryption_key_type: str | None = None
    encryption_key_arn: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> FirehoseStreamProps:
        data = schema.mapping(raw, "firehose stream", {
            "name", "create", "destination", "role", "role_arn", "enable_cloudwatch_logging",
            "cloudwatch_log_retention_days", "s3_configuration", "opensearch_configuration",
            "processing_configuration", "kinesis_source_configuration", "server_side_encryption", "tags",
        }, required=("name",))
        name = schema.name(data["name"], "firehose stream name")
        label = f"Firehose stream '{name}'"
        if "role" in data and "role_arn" in data:
            raise ValueError(f"{label}: provide only one of role or role_arn.")
        destination = _choice(data.get("destination"), f"{label}: destination", DESTINATIONS, "extended_s3")
        if data.get("s3_configuration") is None:
            raise ValueError(f"{label}: s3_configuration is required (destination or OpenSearch backup).")
        s3 = S3Props.from_dict(data["s3_configuration"], f"{label}: s3_configuration")
        opensearch = None
        if destination == "opensearch":
            if data.get("opensearch_configuration") is None:
                raise ValueError(f"{label}: destination opensearch needs opensearch_configuration.")
            opensearch = OpenSearchDestinationProps.from_dict(
                data["opensearch_configuration"], f"{label}: opensearch_configuration"
            )
        elif data.get("opensearch_configuration") is not None:
            raise ValueError(f"{label}: opensearch_configuration needs destination: opensearch.")
        enabled, processors = True, []
        if data.get("processing_configuration") is not None:
            where = f"{label}: processing_configuration"
            processing = schema.mapping(data["processing_configuration"], where, {"enabled", "processors"})
            enabled = schema.boolean(processing.get("enabled"), f"{where}.enabled", default=True)
            for entry in processing.get("processors") or []:
                item = schema.mapping(entry, f"{where}.processors", {"type", "parameters"}, required=("type",))
                kind = _choice(item["type"], f"{where}.processors type", PROCESSOR_TYPES, "Lambda")
                parameters = []
                for parameter in item.get("parameters") or []:
                    value = schema.mapping(parameter, f"{where}.parameters",
                                           {"parameter_name", "parameter_value"},
                                           required=("parameter_name", "parameter_value"))
                    parameters.append((str(value["parameter_name"]), str(value["parameter_value"])))
                processors.append(ProcessorProps(kind, tuple(parameters)))
        kinesis_arn = kinesis_role = None
        if data.get("kinesis_source_configuration") is not None:
            where = f"{label}: kinesis_source_configuration"
            source = schema.mapping(data["kinesis_source_configuration"], where,
                                    {"kinesis_stream_arn", "role_arn"},
                                    required=("kinesis_stream_arn", "role_arn"))
            kinesis_arn = _arn(source["kinesis_stream_arn"], f"{where}: kinesis_stream_arn")
            kinesis_role = _arn(source["role_arn"], f"{where}: role_arn")
        key_type = key_arn = None
        if data.get("server_side_encryption") is not None:
            where = f"{label}: server_side_encryption"
            sse = schema.mapping(data["server_side_encryption"], where, {"enabled", "key_type", "key_arn"})
            if schema.boolean(sse.get("enabled"), f"{where}.enabled", default=True):
                key_type = _choice(sse.get("key_type"), f"{where}.key_type", KEY_TYPES, "AWS_OWNED_CMK")
                key_arn = _arn(sse.get("key_arn"), f"{where}.key_arn")
                if (key_type == "CUSTOMER_MANAGED_CMK") != (key_arn is not None):
                    raise ValueError(f"{where}: key_arn is required with, and only with, CUSTOMER_MANAGED_CMK.")
        if key_type is not None and kinesis_arn is not None:
            raise ValueError(f"{label}: server_side_encryption is not supported with a Kinesis stream source.")
        days = schema.integer(data.get("cloudwatch_log_retention_days"), f"{label}: cloudwatch_log_retention_days",
                              default=14, minimum=1) or 14
        if days not in LOG_RETENTION_DAYS:
            raise ValueError(f"{label}: cloudwatch_log_retention_days must be a CloudWatch retention value.")
        return cls(
            name=name,
            destination=destination,
            role=schema.name(data["role"], f"{label}: role") if "role" in data else None,
            role_arn=_arn(data.get("role_arn"), f"{label}: role_arn"),
            enable_cloudwatch_logging=schema.boolean(
                data.get("enable_cloudwatch_logging"), f"{label}: enable_cloudwatch_logging", default=True
            ),
            log_retention_days=days,
            s3=s3,
            opensearch=opensearch,
            processing_enabled=enabled,
            processors=tuple(processors),
            kinesis_stream_arn=kinesis_arn,
            kinesis_role_arn=kinesis_role,
            encryption_key_type=key_type,
            encryption_key_arn=key_arn,
            tags=schema.string_map(data.get("tags"), f"{label}: tags"),
        )


@dataclass(frozen=True)
class FirehoseProps:
    streams: tuple[FirehoseStreamProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.streams)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> FirehoseProps:
        if raw is not None and not isinstance(raw, list):
            raise ValueError("firehose_streams must be a list.")
        enabled = [item for item in raw or [] if not isinstance(item, dict) or item.get("create", True)]
        return cls(streams=schema.items(enabled, "firehose_streams", FirehoseStreamProps.from_dict))
