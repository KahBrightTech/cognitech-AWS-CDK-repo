import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.config import load_environment
from cognitech_cdk.stacks.network_stack import NetworkStack


def _template(network_name: str) -> Template:
    config = load_environment("uat")
    network = next(n for n in config.networks if n.name == network_name)
    app = cdk.App()
    stack = NetworkStack(
        app,
        f"test-{network_name}",
        common=config.common,
        network=network,
        env=cdk.Environment(account=config.account_id, region=config.region),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def uat() -> Template:
    return _template("uat")


@pytest.fixture(scope="module")
def dmz() -> Template:
    return _template("dmz")


def test_creates_one_vpc_with_dns_enabled(uat: Template):
    uat.resource_count_is("AWS::EC2::VPC", 1)
    uat.has_resource_properties(
        "AWS::EC2::VPC",
        {
            "CidrBlock": "10.20.0.0/16",
            "EnableDnsHostnames": True,
            "EnableDnsSupport": True,
        },
    )


def test_creates_each_group_in_each_az(uat: Template):
    # 3 groups x 2 AZs
    uat.resource_count_is("AWS::EC2::Subnet", 6)


def test_subnets_use_the_configured_cidrs(uat: Template):
    for cidr, zone in (
        ("10.20.0.0/20", "us-east-1a"),
        ("10.20.16.0/20", "us-east-1b"),
        ("10.20.64.0/20", "us-east-1a"),
        ("10.20.128.0/20", "us-east-1a"),
    ):
        uat.has_resource_properties(
            "AWS::EC2::Subnet", {"CidrBlock": cidr, "AvailabilityZone": zone}
        )


def test_public_subnets_route_to_the_internet_gateway(uat: Template):
    uat.resource_count_is("AWS::EC2::InternetGateway", 1)
    uat.has_resource_properties(
        "AWS::EC2::Route",
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": Match.any_value()},
    )


def test_public_subnets_auto_assign_public_ips(uat: Template):
    uat.has_resource_properties(
        "AWS::EC2::Subnet",
        {
            "MapPublicIpOnLaunch": True,
            "Tags": Match.array_with(
                [{"Key": "Name", "Value": "int-production-use1-cdk-uat-edge-public-primary"}]
            ),
        },
    )


def test_private_subnets_route_through_nat(uat: Template):
    uat.resource_count_is("AWS::EC2::NatGateway", 1)
    uat.has_resource_properties(
        "AWS::EC2::Route",
        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": Match.any_value()},
    )


def test_isolated_subnets_have_no_default_route(uat: Template):
    # 2 public (IGW) + 2 private (NAT); the isolated pair gets none.
    routes = uat.find_resources(
        "AWS::EC2::Route", {"Properties": {"DestinationCidrBlock": "0.0.0.0/0"}}
    )
    assert len(routes) == 4


def test_common_tags_are_applied(uat: Template):
    uat.has_resource_properties(
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


@pytest.mark.parametrize(
    "resource_type, name",
    [
        ("AWS::EC2::VPC", "int-production-use1-cdk-uat-vpc"),
        ("AWS::EC2::InternetGateway", "int-production-use1-cdk-uat-igw"),
        ("AWS::EC2::Subnet", "int-production-use1-cdk-uat-app-private-primary"),
        ("AWS::EC2::RouteTable", "int-production-use1-cdk-uat-app-private-primary-rtb"),
        ("AWS::EC2::NatGateway", "int-production-use1-cdk-uat-primary-natgw"),
        ("AWS::EC2::EIP", "int-production-use1-cdk-uat-primary-nat-eip"),
    ],
)
def test_names_follow_the_convention(uat: Template, resource_type: str, name: str):
    uat.has_resource_properties(
        resource_type, {"Tags": Match.array_with([{"Key": "Name", "Value": name}])}
    )


def test_public_only_network_has_no_nat_gateway(dmz: Template):
    dmz.resource_count_is("AWS::EC2::Subnet", 2)
    dmz.resource_count_is("AWS::EC2::InternetGateway", 1)
    dmz.resource_count_is("AWS::EC2::NatGateway", 0)
    dmz.resource_count_is("AWS::EC2::EIP", 0)
