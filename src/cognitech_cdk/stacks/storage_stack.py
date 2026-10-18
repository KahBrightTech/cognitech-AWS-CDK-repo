"""Private S3 buckets (Terraform `s3_app_bucket` / `s3_private_buckets`)."""

from __future__ import annotations

from typing import Mapping

from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.storage import Storage, StorageProps, bucket_logical_name
from cognitech_cdk.stacks.base import EnvironmentStack


class StorageStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: StorageProps,
        roles: Mapping[str, iam.IRole] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        self.storage = Storage(
            self,
            "Storage",
            common=environment.common,
            network_name=environment.network.name,
            props=props,
            roles=roles,
        )
        self.buckets: dict[str, s3.IBucket] = self.storage.buckets

        for bucket_props in props.buckets:
            bucket = self.buckets[bucket_props.name]
            logical = bucket_logical_name(bucket_props.name)
            self.output(
                f"{logical}BucketName", bucket.bucket_name, f"{bucket_props.name}-bucket-name",
                description=bucket_props.description,
            )
            self.output(
                f"{logical}BucketArn", bucket.bucket_arn, f"{bucket_props.name}-bucket-arn",
                description=bucket_props.description,
            )
