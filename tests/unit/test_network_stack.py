import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.config import load_environment
from cognitech_cdk.stacks.network_stack import NetworkStack


@pytest.fixture(scope="module")
def template() -> Template:
    config = load_environment("uat")
    network = config.networks[0]
    app = cdk.App()
    stack = NetworkStack(
        app,
        "test-network",
        common=config.common,
        network=network,
        env=cdk.Environment(account=config.account_id, region=config.region),
    )
    return Template.from_stack(stack)


def test_creates_one_vpc_with_dns_enabled(template: Template):
    template.resource_count_is("AWS::EC2::VPC", 1)
    template.has_resource_properties(
        "AWS::EC2::VPC",
        {
            "CidrBlock": "10.20.0.0/16",
            "EnableDnsHostnames": True,
            "EnableDnsSupport": True,
        },
    )


def test_creates_every_configured_subnet(template: Template):
    template.resource_count_is("AWS::EC2::Subnet", 6)


def test_public_subnets_route_to_the_internet_gateway(template: Template):
    template.resource_count_is("AWS::EC2::InternetGateway", 1)
    template.has_resource_properties(
        "AWS::EC2::Route",
        {
            "DestinationCidrBlock": "0.0.0.0/0",
            "GatewayId": Match.any_value(),
        },
    )


def test_private_subnets_route_through_nat(template: Template):
    template.resource_count_is("AWS::EC2::NatGateway", 1)
    template.has_resource_properties(
        "AWS::EC2::Route",
        {
            "DestinationCidrBlock": "0.0.0.0/0",
            "NatGatewayId": Match.any_value(),
        },
    )


def test_common_tags_are_applied(template: Template):
    template.has_resource_properties(
        "AWS::EC2::VPC",
        {
            "Tags": Match.array_with(
                [
                    {"Key": "Build-method", "Value": "aws-cdk"},
                    {"Key": "Compliance", "Value": "hippaa"},
                    {"Key": "Environment", "Value": "user-acceptance-test"},
                    {"Key": "Owner", "Value": "kbrigthain@gmail.com"},
                ]
            )
        },
    )


def test_vpc_name_follows_the_terraform_convention(template: Template):
    template.has_resource_properties(
        "AWS::EC2::VPC",
        {"Tags": Match.array_with([{"Key": "Name", "Value": "int-production-use1-uat-vpc"}])},
    )


def test_subnet_and_route_table_names_follow_the_convention(template: Template):
    template.has_resource_properties(
        "AWS::EC2::Subnet",
        {
            "Tags": Match.array_with(
                [{"Key": "Name", "Value": "int-production-use1-uat-app-private-primary"}]
            )
        },
    )
    template.has_resource_properties(
        "AWS::EC2::RouteTable",
        {
            "Tags": Match.array_with(
                [{"Key": "Name", "Value": "int-production-use1-uat-app-private-primary-rtb"}]
            )
        },
    )
