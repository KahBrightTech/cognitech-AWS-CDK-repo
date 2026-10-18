"""Kinesis Data Firehose delivery streams to S3 or OpenSearch."""

from __future__ import annotations

from typing import Mapping

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kinesisfirehose as firehose
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_s3 as s3
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.ecr import logical_id
from cognitech_cdk.constructs.firehose import STREAM_NAME, FirehoseProps, FirehoseStreamProps
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.stacks.base import EnvironmentStack

LOG_STREAM = "DestinationDelivery"


class FirehoseStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: FirehoseProps,
        buckets: Mapping[str, s3.IBucket] | None = None,
        domains: Mapping[str, opensearch.IDomain] | None = None,
        roles: Mapping[str, iam.IRole] | None = None,
        network: Network | None = None,
        security_groups: Mapping[str, ec2.ISecurityGroup] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        self._buckets = buckets or {}
        self._domains = domains or {}
        self._roles = roles or {}
        self._network = network
        self._security_groups = security_groups or {}
        self.streams: dict[str, firehose.CfnDeliveryStream] = {}
        self.delivery_roles: dict[str, iam.IRole] = {}
        for stream in props.streams:
            self._stream(stream)

    def _arn(self, values: Mapping, key: str | None, arn: str | None, attribute: str, label: str) -> str:
        return getattr(schema.lookup(values, key, label), attribute) if key is not None else arn

    def _stream(self, props: FirehoseStreamProps) -> None:
        label = f"Firehose stream '{props.name}'"
        name = self.resource_name(props.name)
        if not STREAM_NAME.fullmatch(name):
            raise ValueError(f"{label}: stream name '{name}' must be 1-64 characters.")
        cid = logical_id(props.name)
        scope = Construct(self, cid)
        assert props.s3 is not None
        bucket_arn = self._arn(self._buckets, props.s3.bucket, props.s3.bucket_arn, "bucket_arn",
                               f"{label}: s3_configuration.bucket")
        domain_arn = None
        if props.opensearch is not None:
            domain_arn = self._arn(self._domains, props.opensearch.domain, props.opensearch.domain_arn,
                                   "domain_arn", f"{label}: opensearch_configuration.domain")

        logging = None
        log_group = None
        if props.enable_cloudwatch_logging:
            log_group = logs.CfnLogGroup(
                scope, "LogGroup",
                log_group_name=f"/aws/kinesisfirehose/{name}",
                retention_in_days=props.log_retention_days,
            )
            log_group.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
            log_stream = logs.CfnLogStream(
                scope, "LogStream", log_group_name=log_group.ref, log_stream_name=LOG_STREAM
            )
            log_stream.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
            logging = firehose.CfnDeliveryStream.CloudWatchLoggingOptionsProperty(
                enabled=True, log_group_name=log_group.ref, log_stream_name=LOG_STREAM
            )

        if props.role is not None:
            role_arn = schema.lookup(self._roles, props.role, f"{label}: role").role_arn
        elif props.role_arn is not None:
            role_arn = props.role_arn
        else:
            role = self._delivery_role(scope, props, bucket_arn, domain_arn, log_group)
            self.delivery_roles[props.name] = role
            role_arn = role.role_arn

        s3_props = props.s3
        processing = (
            firehose.CfnDeliveryStream.ProcessingConfigurationProperty(
                enabled=props.processing_enabled,
                processors=[
                    firehose.CfnDeliveryStream.ProcessorProperty(
                        type=processor.type,
                        parameters=[
                            firehose.CfnDeliveryStream.ProcessorParameterProperty(
                                parameter_name=key, parameter_value=value
                            )
                            for key, value in processor.parameters
                        ] or None,
                    )
                    for processor in props.processors
                ],
            )
            if props.processors else None
        )
        s3_destination = dict(
            role_arn=role_arn,
            bucket_arn=bucket_arn,
            prefix=s3_props.prefix or None,
            error_output_prefix=s3_props.error_output_prefix,
            buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                size_in_m_bs=s3_props.buffering_size, interval_in_seconds=s3_props.buffering_interval
            ),
            compression_format=s3_props.compression_format,
            cloud_watch_logging_options=logging,
        )

        extended = None
        amazon_opensearch = None
        if props.destination == "extended_s3":
            extended = firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                **s3_destination, processing_configuration=processing
            )
        else:
            target = props.opensearch
            assert target is not None
            vpc = None
            if target.security_groups:
                if self._network is None:
                    raise ValueError(f"{label}: vpc_config needs the network.")
                subnets = (self._network.private_subnets if target.subnets == "private"
                           else self._network.public_subnets)
                if not subnets:
                    raise ValueError(f"{label}: the network has no {target.subnets} subnets.")
                vpc = firehose.CfnDeliveryStream.VpcConfigurationProperty(
                    role_arn=role_arn,
                    subnet_ids=[subnet.subnet_id for subnet in subnets],
                    security_group_ids=[
                        schema.lookup(self._security_groups, key, f"{label}: security group").security_group_id
                        for key in target.security_groups
                    ],
                )
            amazon_opensearch = firehose.CfnDeliveryStream.AmazonopensearchserviceDestinationConfigurationProperty(
                role_arn=role_arn,
                domain_arn=domain_arn,
                index_name=target.index_name,
                index_rotation_period=target.index_rotation_period,
                type_name=target.type_name,
                buffering_hints=firehose.CfnDeliveryStream.AmazonopensearchserviceBufferingHintsProperty(
                    size_in_m_bs=target.buffering_size, interval_in_seconds=target.buffering_interval
                ),
                retry_options=firehose.CfnDeliveryStream.AmazonopensearchserviceRetryOptionsProperty(
                    duration_in_seconds=target.retry_duration
                ),
                s3_backup_mode=target.s3_backup_mode,
                s3_configuration=firehose.CfnDeliveryStream.S3DestinationConfigurationProperty(
                    **s3_destination
                ),
                cloud_watch_logging_options=logging,
                processing_configuration=processing,
                vpc_configuration=vpc,
            )

        stream = firehose.CfnDeliveryStream(
            scope,
            "DeliveryStream",
            delivery_stream_name=name,
            delivery_stream_type="KinesisStreamAsSource" if props.kinesis_stream_arn else "DirectPut",
            kinesis_stream_source_configuration=(
                firehose.CfnDeliveryStream.KinesisStreamSourceConfigurationProperty(
                    kinesis_stream_arn=props.kinesis_stream_arn, role_arn=props.kinesis_role_arn
                )
                if props.kinesis_stream_arn else None
            ),
            delivery_stream_encryption_configuration_input=(
                firehose.CfnDeliveryStream.DeliveryStreamEncryptionConfigurationInputProperty(
                    key_type=props.encryption_key_type, key_arn=props.encryption_key_arn
                )
                if props.encryption_key_type else None
            ),
            extended_s3_destination_configuration=extended,
            amazon_open_search_serverless_destination_configuration=None,
            amazonopensearchservice_destination_configuration=amazon_opensearch,
        )
        cdk.Tags.of(stream).add("Name", name)
        for key, value in props.tags.items():
            cdk.Tags.of(stream).add(key, value)
        if props.name in self.delivery_roles:
            # IAM is eventually consistent; Firehose validates the role on create.
            stream.node.add_dependency(self.delivery_roles[props.name])
        self.streams[props.name] = stream
        self.output(f"{cid}StreamName", stream.ref, f"{props.name}-firehose-name")
        self.output(f"{cid}StreamArn", stream.attr_arn, f"{props.name}-firehose-arn")

    def _delivery_role(
        self, scope: Construct, props: FirehoseStreamProps, bucket_arn: str,
        domain_arn: str | None, log_group: logs.CfnLogGroup | None,
    ) -> iam.Role:
        role = iam.Role(
            scope, "DeliveryRole",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
            description=f"Delivery role for Firehose stream {props.name}",
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:GetObject",
                "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutObject",
            ],
            resources=[bucket_arn, f"{bucket_arn}/*"],
        ))
        if domain_arn is not None:
            role.add_to_policy(iam.PolicyStatement(
                actions=["es:DescribeDomain", "es:DescribeDomains", "es:DescribeDomainConfig",
                         "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpGet"],
                resources=[domain_arn, f"{domain_arn}/*"],
            ))
            if props.opensearch is not None and props.opensearch.security_groups:
                role.add_to_policy(iam.PolicyStatement(
                    actions=["ec2:DescribeVpcs", "ec2:DescribeVpcAttribute", "ec2:DescribeSubnets",
                             "ec2:DescribeSecurityGroups", "ec2:DescribeNetworkInterfaces",
                             "ec2:CreateNetworkInterface", "ec2:CreateNetworkInterfacePermission",
                             "ec2:DeleteNetworkInterface"],
                    resources=["*"],
                ))
        if log_group is not None:
            # CloudFormation's log group Arn attribute already ends in ":*".
            role.add_to_policy(iam.PolicyStatement(
                actions=["logs:PutLogEvents", "logs:CreateLogStream"],
                resources=[log_group.attr_arn],
            ))
        lambdas = [
            value for processor in props.processors if processor.type == "Lambda"
            for key, value in processor.parameters if key == "LambdaArn"
        ]
        if lambdas:
            role.add_to_policy(iam.PolicyStatement(
                actions=["lambda:InvokeFunction", "lambda:GetFunctionConfiguration"],
                resources=[arn.split(":$")[0] + "*" for arn in lambdas],
            ))
        if props.encryption_key_arn is not None:
            role.add_to_policy(iam.PolicyStatement(
                actions=["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"],
                resources=[props.encryption_key_arn],
            ))
        return role
