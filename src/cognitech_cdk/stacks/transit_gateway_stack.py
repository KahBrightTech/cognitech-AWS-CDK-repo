"""Transit gateway: the gateway, this VPC's attachment and the routes through it.

Separate from the network stack so routing changes never touch the VPC. The
VPC, subnets and subnet route tables arrive from `Network` as cross-stack
references.
"""

from __future__ import annotations

from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.constructs.transit_gateway import (
    TransitGateway,
    TransitGatewayProps,
    construct_id,
    export_suffix,
)
from cognitech_cdk.stacks.base import EnvironmentStack


class TransitGatewayStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        environment: Environment,
        props: TransitGatewayProps,
        network: Network,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id_, environment=environment, **kwargs)
        self.props = props

        self.transit_gateway = TransitGateway(
            self,
            "TransitGateway",
            common=environment.common,
            network_name=environment.network.name,
            props=props,
            network=network,
        )
        resources = self.transit_gateway

        self.gateway = resources.gateway
        self.transit_gateway_id = resources.transit_gateway_id
        self.transit_gateway_arn = resources.transit_gateway_arn
        self.resource_share = resources.resource_share
        self.attachments = resources.attachments
        self.attachment_ids = {name: item.ref for name, item in self.attachments.items()}
        self.route_tables = resources.route_tables
        self.route_table_ids = {key: item.ref for key, item in self.route_tables.items()}
        self.associations = resources.associations
        self.propagations = resources.propagations
        self.routes = resources.routes
        self.subnet_routes = resources.subnet_routes

        if self.transit_gateway_id:
            self.output(
                "TransitGatewayId", self.transit_gateway_id, "tgw-id",
                description="Transit gateway id",
            )
        if self.transit_gateway_arn:
            self.output(
                "TransitGatewayArn", self.transit_gateway_arn, "tgw-arn",
                description="Transit gateway ARN",
            )
        if self.resource_share:
            self.output(
                "TransitGatewayResourceShareArn", self.resource_share.attr_arn,
                "tgw-ram-share-arn", description="RAM share for the transit gateway",
            )
        for name, attachment_id in self.attachment_ids.items():
            self.output(
                f"{construct_id(name)}AttachmentId", attachment_id,
                f"{export_suffix(name)}-tgw-attachment-id",
                description=f"Transit gateway attachment '{name}'",
            )
        for key, route_table_id in self.route_table_ids.items():
            self.output(
                f"{construct_id(key)}RouteTableId", route_table_id,
                f"{export_suffix(key)}-tgw-rtb-id",
                description=f"Transit gateway route table '{key}'",
            )
