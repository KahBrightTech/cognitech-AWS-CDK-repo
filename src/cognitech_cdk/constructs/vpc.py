"""VPC construct"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.config import CommonProps


class Vpc(Construct):
    """Creates a VPC plus an internet gateway and its attachment.

    Subnets are intentionally *not* created here so that CIDRs and AZs stay
    explicit in config, exactly like the Terraform subnet modules.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: CommonProps,
        name: str,
        cidr_block: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=common.resource_name(name, "vpc"),
            ip_addresses=ec2.IpAddresses.cidr(cidr_block),
            enable_dns_hostnames=True,
            enable_dns_support=True,
            create_internet_gateway=False,
            nat_gateways=0,
            subnet_configuration=[],
        )

        self.internet_gateway = ec2.CfnInternetGateway(
            self,
            "InternetGateway",
            tags=[cdk.CfnTag(key="Name", value=common.resource_name(name, "igw"))],
        )

        # Kept as an attribute so public subnet routes can depend on it.
        self.internet_gateway_attachment = ec2.CfnVPCGatewayAttachment(
            self,
            "InternetGatewayAttachment",
            vpc_id=self.vpc.vpc_id,
            internet_gateway_id=self.internet_gateway.ref,
        )

    @property
    def vpc_id(self) -> str:
        return self.vpc.vpc_id

    @property
    def internet_gateway_id(self) -> str:
        return self.internet_gateway.ref
