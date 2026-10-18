"""Transit gateway family: parsing, synthesized resources, references and outputs."""

from __future__ import annotations

import textwrap

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.transit_gateway import TransitGatewayProps
from cognitech_cdk.stacks.network_stack import NetworkStack
from cognitech_cdk.stacks.transit_gateway_stack import TransitGatewayStack

WITH_PRIVATE = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: true
"""

PUBLIC_ONLY = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

FULL = """
gateway:
  name: shared
  amazon_side_asn: 64600
  default_route_table_association: disable
  default_route_table_propagation: false
attachments:
  - name: uat
route_tables:
  - key: shared
    name: shared-services
  - key: spoke
    name: spoke
associations:
  - key: uat-shared
    attachment: uat
    route_table: shared
propagations:
  - key: uat-spoke
    attachment: uat
    route_table: spoke
  - key: external
    attachment_id: tgw-attach-0123456789abcdef0
    route_table: shared
routes:
  - key: on-prem
    destination_cidr_block: 10.0.0.0/8
    route_table: shared
  - key: drop
    destination_cidr_block: 192.168.0.0/16
    blackhole: true
    route_table_id: tgw-rtb-0123456789abcdef0
subnet_routes:
  - name: corporate
    cidr_block: 10.0.0.0/8
  - name: partner
    cidr_block: 172.16.0.0/12
    availability_zones: [secondary]
"""


def _props(text: str) -> TransitGatewayProps:
    return TransitGatewayProps.from_dict(yaml.safe_load(textwrap.dedent(text)))


def _build(write_env, config: str, network: str = WITH_PRIVATE):
    environment = load_environment("test", root=write_env(network))
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network_stack = NetworkStack(app, "network", environment=environment, env=env)
    stack = TransitGatewayStack(
        app, "tgw", environment=environment, props=_props(config),
        network=network_stack.network, env=env,
    )
    return app, network_stack, stack


def _template(write_env, config: str, network: str = WITH_PRIVATE) -> Template:
    return Template.from_stack(_build(write_env, config, network)[2])


def _name(resource: dict) -> str:
    return next(t["Value"] for t in resource["Properties"]["Tags"] if t["Key"] == "Name")


def _resources(template: Template, kind: str) -> dict[str, dict]:
    return template.find_resources(kind)


# --- parsing ----------------------------------------------------------------


def test_missing_section_is_empty_and_falsy():
    assert not TransitGatewayProps.from_dict(None)
    assert not TransitGatewayProps.from_dict({})
    # An existing id on its own creates nothing.
    assert not _props("transit_gateway_id: tgw-0123456789abcdef0")
    assert _props("gateway: {name: shared}")


def test_defaults_follow_aws_and_stay_safe():
    gateway = _props("gateway: {name: shared}").gateway
    assert gateway.amazon_side_asn == 64512
    assert gateway.default_route_table_association == "enable"
    assert gateway.auto_accept_shared_attachments == "disable"
    assert gateway.dns_support == "enable"
    assert gateway.ram is None


def test_only_attachment_and_route_table_are_used_by_default():
    props = _props("""
        gateway: {name: shared, default_route_table_association: disable}
        attachments: [{name: uat}]
        route_tables: [{name: shared}]
        associations: [{key: a}]
        routes: [{key: r, destination_cidr_block: 10.0.0.0/8}]
    """)
    assert props.associations[0].attachment == "uat"
    assert props.associations[0].route_table == "shared"
    assert props.routes[0].attachment == "uat"
    assert props.attachments[0].subnet_type == "private"


@pytest.mark.parametrize(
    "config, message",
    [
        ("color: blue", "unknown fields: color"),
        ("gateway: {name: shared, ram_share: true}", "unknown fields: ram_share"),
        ("gateway: {name: shared}\ntransit_gateway_id: tgw-0abc", "not both"),
        ("transit_gateway_id: vpc-0abc", "id like tgw-"),
        ("gateway: {name: shared, amazon_side_asn: 100}", "private ASN"),
        ("gateway: {name: shared, dns_support: maybe}", "enable, disable"),
        ("attachments: [{name: uat}]", "no transit gateway"),
        ("route_tables: [{name: shared}]", "no transit gateway"),
        ("gateway: {name: g}\nattachments: [{name: a}, {name: A}]", "duplicate name"),
        ("gateway: {name: g}\nroute_tables: [{name: a-b}, {name: a_b}]", "differ only"),
        ("gateway: {name: g}\nattachments: [{name: a}, {name: b}]", "same VPC to the same"),
        ("gateway: {name: g}\nattachments: [{name: a, subnet_type: isolated}]", "public or private"),
        (
            "gateway: {name: g}\nroute_tables: [{name: r}]\n"
            "associations: [{key: x, attachment: missing}]",
            "unknown reference 'missing'",
        ),
        (
            "gateway: {name: g}\nattachments: [{name: a}]\nroute_tables: [{name: r}, {name: s}]\n"
            "associations: [{key: x}]",
            "needs route_table or route_table_id",
        ),
        (
            "gateway: {name: g}\nattachments: [{name: a}]\nroute_tables: [{name: r}]\n"
            "associations: [{key: x, route_table: r, route_table_id: tgw-rtb-0abc}]",
            "not both",
        ),
        (
            "gateway: {name: g}\nattachments: [{name: a}]\nroute_tables: [{name: r}]\n"
            "associations: [{key: x}]",
            "default_route_table_association: disable",
        ),
        (
            "gateway: {name: g, default_route_table_association: disable}\n"
            "attachments: [{name: a}]\nroute_tables: [{name: r}, {name: s}]\n"
            "associations: [{key: x, route_table: r}, {key: y, route_table: s}]",
            "already associated",
        ),
        (
            "gateway: {name: g}\nattachments: [{name: a}]\n"
            "route_tables: [{name: r, transit_gateway_id: tgw-0abc}]\n"
            "propagations: [{key: x}]",
            "different transit gateways",
        ),
        (
            "gateway: {name: g}\nroute_tables: [{name: r}]\n"
            "routes: [{key: x, destination_cidr_block: 10.0.0.0/8}]",
            "needs attachment or attachment_id",
        ),
        (
            "gateway: {name: g}\nattachments: [{name: a}]\nroute_tables: [{name: r}]\n"
            "routes: [{key: x, destination_cidr_block: 10.0.0.0/8, blackhole: true, attachment: a}]",
            "blackhole route must not",
        ),
        (
            "gateway: {name: g}\nattachments: [{name: a}]\nroute_tables: [{name: r}]\n"
            "routes: [{key: x, destination_cidr_block: 10.0.0.1/8}]",
            "not a valid CIDR",
        ),
        (
            "gateway: {name: g}\nattachments: [{name: a}]\nroute_tables: [{name: r}]\n"
            "routes: [{key: x, destination_cidr_block: 10.0.0.0/8},"
            " {key: y, destination_cidr_block: 10.0.0.0/8}]",
            "already routes",
        ),
        ("gateway: {name: g}\nsubnet_routes: [{name: s, cidr_block: 0.0.0.0/0}]", "default route"),
        (
            "gateway: {name: g}\nsubnet_routes: [{name: s, cidr_block: 10.0.0.0/8,"
            " availability_zones: [fifth]}]",
            "availability zone 'fifth'",
        ),
        (
            "gateway: {name: g}\nsubnet_routes: [{name: s, cidr_block: 10.0.0.0/8,"
            " availability_zones: [primary, 0]}]",
            "repeated",
        ),
        (
            "gateway: {name: g, ram: {enabled: true, principals: ['*']}}",
            "12-digit account id",
        ),
    ],
)
def test_bad_config_is_rejected(config, message):
    with pytest.raises(ValueError, match=message):
        _props(config)


# --- what gets built ----------------------------------------------------------


def test_gateway_mirrors_the_terraform_module(write_env):
    template = _template(write_env, FULL)

    template.resource_count_is("AWS::EC2::TransitGateway", 1)
    gateway = next(iter(_resources(template, "AWS::EC2::TransitGateway").values()))
    properties = gateway["Properties"]
    assert properties["AmazonSideAsn"] == 64600
    assert properties["DefaultRouteTableAssociation"] == "disable"
    assert properties["DefaultRouteTablePropagation"] == "disable"
    assert properties["AutoAcceptSharedAttachments"] == "disable"
    assert properties["DnsSupport"] == "enable"
    assert properties["Description"] == "The main transit gateway for the account"
    assert _name(gateway) == "int-production-use1-uat-shared-tgw"
    assert {"Key": "Compliance", "Value": "hippaa"} in properties["Tags"]


def test_attachment_uses_one_private_subnet_per_az(write_env):
    _, _, stack = _build(write_env, FULL)
    template = Template.from_stack(stack)

    attachments = _resources(template, "AWS::EC2::TransitGatewayVpcAttachment")
    assert len(attachments) == 1
    attachment = next(iter(attachments.values()))
    properties = attachment["Properties"]
    assert _name(attachment) == "int-production-use1-uat-uat-tgw-attach"
    assert properties["TransitGatewayId"] == {"Ref": stack.get_logical_id(stack.gateway)}
    assert "Fn::ImportValue" in properties["VpcId"]
    subnet_exports = [subnet["Fn::ImportValue"] for subnet in properties["SubnetIds"]]
    assert len(subnet_exports) == 2
    assert all("PrivateSubnet" in name for name in subnet_exports)


def test_attachment_can_use_public_subnets(write_env):
    template = _template(
        write_env,
        "transit_gateway_id: tgw-0123456789abcdef0\n"
        "attachments: [{name: edge, subnet_type: public}]",
        PUBLIC_ONLY,
    )

    properties = next(iter(
        _resources(template, "AWS::EC2::TransitGatewayVpcAttachment").values()
    ))["Properties"]
    assert properties["TransitGatewayId"] == "tgw-0123456789abcdef0"
    assert all("PublicSubnet" in s["Fn::ImportValue"] for s in properties["SubnetIds"])
    template.resource_count_is("AWS::EC2::TransitGateway", 0)


def test_private_attachment_without_private_subnets_is_explained(write_env):
    with pytest.raises(ValueError, match="network 'uat' has no private subnets"):
        _build(write_env, "gateway: {name: g}\nattachments: [{name: uat}]", PUBLIC_ONLY)


def test_private_subnet_route_without_private_subnets_is_explained(write_env):
    with pytest.raises(ValueError, match="has no private subnets"):
        _build(
            write_env,
            "gateway: {name: g}\nsubnet_routes: [{name: s, cidr_block: 10.0.0.0/8}]",
            PUBLIC_ONLY,
        )


def test_route_tables_associations_and_propagations_reference_each_other(write_env):
    _, _, stack = _build(write_env, FULL)
    template = Template.from_stack(stack)
    resolve = stack.resolve

    names = {
        _name(table)
        for table in _resources(template, "AWS::EC2::TransitGatewayRouteTable").values()
    }
    assert names == {
        "int-production-use1-uat-shared-services-tgw-rtb",
        "int-production-use1-uat-spoke-tgw-rtb",
    }

    association = stack.associations["uat-shared"]
    assert resolve(association.transit_gateway_attachment_id) == resolve(
        stack.attachments["uat"].ref
    )
    assert resolve(association.transit_gateway_route_table_id) == resolve(
        stack.route_tables["shared"].ref
    )
    template.resource_count_is("AWS::EC2::TransitGatewayRouteTablePropagation", 2)
    external = stack.propagations["external"]
    assert external.transit_gateway_attachment_id == "tgw-attach-0123456789abcdef0"
    assert resolve(external.transit_gateway_route_table_id) == resolve(
        stack.route_tables["shared"].ref
    )


def test_transit_gateway_routes(write_env):
    _, _, stack = _build(write_env, FULL)
    template = Template.from_stack(stack)

    template.resource_count_is("AWS::EC2::TransitGatewayRoute", 2)
    template.has_resource_properties(
        "AWS::EC2::TransitGatewayRoute",
        {
            "DestinationCidrBlock": "10.0.0.0/8",
            "Blackhole": False,
            "TransitGatewayAttachmentId": {"Ref": Match.string_like_regexp("AttachmentUat")},
            "TransitGatewayRouteTableId": {"Ref": Match.string_like_regexp("RouteTableShared")},
        },
    )
    template.has_resource_properties(
        "AWS::EC2::TransitGatewayRoute",
        {
            "DestinationCidrBlock": "192.168.0.0/16",
            "Blackhole": True,
            "TransitGatewayAttachmentId": Match.absent(),
            "TransitGatewayRouteTableId": "tgw-rtb-0123456789abcdef0",
        },
    )


def test_subnet_routes_target_the_cdk_subnet_route_tables(write_env):
    _, _, stack = _build(write_env, FULL)
    template = Template.from_stack(stack)

    routes = _resources(template, "AWS::EC2::Route")
    assert len(routes) == 3
    assert [len(stack.subnet_routes[name]) for name in ("corporate", "partner")] == [2, 1]

    def table(resource: dict) -> str:
        return resource["Properties"]["RouteTableId"]["Fn::ImportValue"]

    corporate = [r for r in routes.values() if r["Properties"]["DestinationCidrBlock"] == "10.0.0.0/8"]
    assert sorted("PrivateSubnetPrimary" in table(r) for r in corporate) == [False, True]
    assert all("PrivateSubnet" in table(r) for r in corporate)
    partner = [r for r in routes.values() if r["Properties"]["DestinationCidrBlock"] == "172.16.0.0/12"]
    assert "PrivateSubnetSecondary" in table(partner[0])

    attachment_id = stack.get_logical_id(stack.attachments["uat"])
    for resource in routes.values():
        assert resource["Properties"]["TransitGatewayId"] == {
            "Ref": stack.get_logical_id(stack.gateway)
        }
        # The VPC has to be attached before a route can point at the gateway.
        assert attachment_id in resource["DependsOn"]


def test_subnet_route_to_a_missing_az_is_explained(write_env):
    with pytest.raises(ValueError, match="tertiary .* does not exist"):
        _build(
            write_env,
            "gateway: {name: g}\nsubnet_routes: [{name: s, cidr_block: 10.0.0.0/8,"
            " availability_zones: [tertiary]}]",
        )


def test_overlapping_subnet_routes_are_rejected(write_env):
    with pytest.raises(ValueError, match="already routes 10.0.0.0/8"):
        _build(
            write_env,
            "gateway: {name: g}\nsubnet_routes: [{name: a, cidr_block: 10.0.0.0/8},"
            " {name: b, cidr_block: 10.0.0.0/8, availability_zones: [1]}]",
        )


def test_ram_share_is_scoped_to_listed_principals(write_env):
    template = _template(
        write_env,
        "gateway: {name: g, ram: {enabled: true, principals: ['444455556666']}}",
    )

    template.has_resource_properties(
        "AWS::RAM::ResourceShare",
        {
            "Name": "int-production-use1-uat-transit-gateway-share",
            "AllowExternalPrincipals": False,
            "Principals": ["444455556666"],
            "ResourceArns": [Match.object_like({"Fn::Join": Match.any_value()})],
        },
    )


def test_disabled_ram_creates_nothing(write_env):
    template = _template(write_env, "gateway: {name: g, ram: {enabled: false}}")
    template.resource_count_is("AWS::RAM::ResourceShare", 0)


# --- outputs and dependencies -------------------------------------------------


def test_ids_are_public_and_exported(write_env):
    _, _, stack = _build(write_env, FULL)
    outputs = Template.from_stack(stack).find_outputs("*")

    exports = {output["Export"]["Name"] for output in outputs.values()}
    assert exports == {
        "tgw-tgw-id",
        "tgw-tgw-arn",
        "tgw-uat-tgw-attachment-id",
        "tgw-shared-tgw-rtb-id",
        "tgw-spoke-tgw-rtb-id",
    }
    assert set(stack.attachment_ids) == {"uat"}
    assert set(stack.route_table_ids) == {"shared", "spoke"}
    assert stack.resolve(stack.transit_gateway_id) == {"Ref": stack.get_logical_id(stack.gateway)}


def test_existing_gateway_id_is_used_everywhere(write_env):
    _, _, stack = _build(
        write_env,
        """
        transit_gateway_id: tgw-0123456789abcdef0
        attachments: [{name: uat}]
        route_tables: [{name: shared}]
        associations: [{key: uat}]
        subnet_routes: [{name: corp, cidr_block: 10.0.0.0/8}]
        """,
    )
    template = Template.from_stack(stack)

    assert stack.gateway is None and stack.transit_gateway_arn is None
    assert stack.transit_gateway_id == "tgw-0123456789abcdef0"
    template.has_resource_properties(
        "AWS::EC2::TransitGatewayRouteTable", {"TransitGatewayId": "tgw-0123456789abcdef0"}
    )
    template.has_resource_properties(
        "AWS::EC2::Route", {"TransitGatewayId": "tgw-0123456789abcdef0"}
    )
    outputs = template.find_outputs("*")
    assert outputs["TransitGatewayId"]["Value"] == "tgw-0123456789abcdef0"
    assert "TransitGatewayArn" not in outputs


def test_the_stack_depends_on_the_network(write_env):
    app, network, stack = _build(write_env, FULL)

    assembly = app.synth()
    assert network in stack.dependencies
    assert "network" in [
        artifact.id for artifact in assembly.get_stack_by_name("tgw").dependencies
    ]
