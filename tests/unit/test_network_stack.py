import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import list_environments, load_environment
from cognitech_cdk.stacks.network_stack import NetworkStack


def _template(environment, outdir=None) -> Template:
    app = cdk.App(outdir=str(outdir) if outdir else None)
    stack = NetworkStack(
        app,
        environment.common.name(environment.network.name, "network"),
        environment=environment,
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    return Template.from_stack(stack)


def _synth(write_env, network: str) -> Template:
    return _template(load_environment("test", root=write_env(network)))


def _names(template: Template, resource_type: str) -> set[str]:
    return {
        tag["Value"]
        for resource in template.find_resources(resource_type).values()
        for tag in resource["Properties"].get("Tags", [])
        if tag["Key"] == "Name"
    }


PUBLIC_ONLY = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

WITH_PRIVATE = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: true
"""


# --- public only ----------------------------------------------------------


def test_public_only_builds_vpc_igw_and_subnets(write_env):
    template = _synth(write_env, PUBLIC_ONLY)

    template.resource_count_is("AWS::EC2::VPC", 1)
    template.resource_count_is("AWS::EC2::InternetGateway", 1)
    template.resource_count_is("AWS::EC2::Subnet", 2)
    template.resource_count_is("AWS::EC2::RouteTable", 2)
    template.has_resource_properties(
        "AWS::EC2::VPC",
        {"CidrBlock": "10.20.0.0/16", "EnableDnsHostnames": True, "EnableDnsSupport": True},
    )


def test_public_only_has_no_nat_gateways_or_eips(write_env):
    template = _synth(write_env, PUBLIC_ONLY)

    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::EIP", 0)
    assert set(template.find_outputs("*")) == {"VpcId", "PublicSubnetIds"}


def test_public_subnets_route_to_the_internet_gateway(write_env):
    template = _synth(write_env, PUBLIC_ONLY)

    template.has_resource_properties("AWS::EC2::Subnet", {"MapPublicIpOnLaunch": True})
    template.has_resource_properties(
        "AWS::EC2::Route",
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": Match.any_value()},
    )


# --- with private subnets -------------------------------------------------


def test_private_subnets_add_nat_gateways_and_eips(write_env):
    template = _synth(write_env, WITH_PRIVATE)

    template.resource_count_is("AWS::EC2::Subnet", 4)
    template.resource_count_is("AWS::EC2::NatGateway", 2)
    template.resource_count_is("AWS::EC2::EIP", 2)
    template.has_resource_properties(
        "AWS::EC2::Route",
        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": Match.any_value()},
    )
    assert set(template.find_outputs("*")) == {"VpcId", "PublicSubnetIds", "PrivateSubnetIds"}


def test_one_nat_gateway_serves_every_private_subnet(write_env):
    template = _synth(
        write_env,
        """
        name: uat
        cidr_block: 10.20.0.0/16
        azs: 3
        private_subnets: true
        nat_gateways: 1
        """,
    )

    template.resource_count_is("AWS::EC2::NatGateway", 1)
    template.resource_count_is("AWS::EC2::EIP", 1)
    # 3 public routes to the IGW + 3 private routes to the single NAT gateway.
    routes = template.find_resources(
        "AWS::EC2::Route", {"Properties": {"DestinationCidrBlock": "0.0.0.0/0"}}
    )
    assert len(routes) == 6


# --- availability zones ---------------------------------------------------


@pytest.mark.parametrize("azs", [1, 2, 3, 4])
def test_az_count_controls_the_subnet_count(write_env, azs):
    template = _synth(
        write_env,
        f"name: uat\ncidr_block: 10.20.0.0/16\nazs: {azs}\nprivate_subnets: true",
    )

    template.resource_count_is("AWS::EC2::Subnet", azs * 2)
    template.resource_count_is("AWS::EC2::NatGateway", azs)
    for letter in "abcd"[:azs]:
        template.has_resource_properties(
            "AWS::EC2::Subnet", {"AvailabilityZone": f"us-east-1{letter}"}
        )


# --- naming convention ----------------------------------------------------


def test_subnets_follow_the_terraform_naming_convention(write_env):
    template = _synth(write_env, WITH_PRIVATE)

    assert _names(template, "AWS::EC2::Subnet") == {
        "int-production-use1-uat-public-primary",
        "int-production-use1-uat-public-secondary",
        "int-production-use1-uat-private-primary",
        "int-production-use1-uat-private-secondary",
    }


def test_custom_subnet_names_appear_in_resource_names(write_env):
    template = _synth(
        write_env,
        """
        name: uat
        cidr_block: 10.20.0.0/16
        azs: 1
        private_subnets: true
        public_subnet_name: edge
        private_subnet_name: app
        """,
    )

    assert _names(template, "AWS::EC2::Subnet") == {
        "int-production-use1-uat-edge-public-primary",
        "int-production-use1-uat-app-private-primary",
    }
    assert _names(template, "AWS::EC2::NatGateway") == {
        "int-production-use1-uat-edge-ngw-primary"
    }
    assert _names(template, "AWS::EC2::EIP") == {"int-production-use1-uat-edge-eip-primary"}
    assert _names(template, "AWS::EC2::RouteTable") == {
        "int-production-use1-uat-edge-primary-public-rt",
        "int-production-use1-uat-app-primary-private-rt",
    }


def test_vpc_igw_and_route_tables_are_named(write_env):
    template = _synth(write_env, WITH_PRIVATE)

    assert _names(template, "AWS::EC2::VPC") == {"int-production-use1-uat-vpc"}
    assert _names(template, "AWS::EC2::InternetGateway") == {"int-production-use1-uat-igw"}
    assert _names(template, "AWS::EC2::NatGateway") == {
        "int-production-use1-uat-ngw-primary",
        "int-production-use1-uat-ngw-secondary",
    }
    assert _names(template, "AWS::EC2::RouteTable") == {
        "int-production-use1-uat-primary-public-rt",
        "int-production-use1-uat-secondary-public-rt",
        "int-production-use1-uat-primary-private-rt",
        "int-production-use1-uat-secondary-private-rt",
    }


# --- tags -----------------------------------------------------------------


# --- subnet CIDRs ---------------------------------------------------------


def _cidrs(template: Template) -> dict[str, str]:
    """Map each subnet's Name tag to its CIDR block."""
    return {
        next(t["Value"] for t in r["Properties"]["Tags"] if t["Key"] == "Name"): r[
            "Properties"
        ]["CidrBlock"]
        for r in template.find_resources("AWS::EC2::Subnet").values()
    }


def test_explicit_cidrs_reach_the_template(write_env):
    template = _synth(
        write_env,
        """
        name: uat
        cidr_block: 10.20.0.0/16
        azs: 2
        private_subnets: true
        public_subnet_cidrs:  [10.20.100.0/24, 10.20.101.0/24]
        private_subnet_cidrs: [10.20.200.0/23, 10.20.202.0/23]
        """,
    )

    assert _cidrs(template) == {
        "int-production-use1-uat-public-primary": "10.20.100.0/24",
        "int-production-use1-uat-public-secondary": "10.20.101.0/24",
        "int-production-use1-uat-private-primary": "10.20.200.0/23",
        "int-production-use1-uat-private-secondary": "10.20.202.0/23",
    }


def test_carved_cidrs_avoid_the_explicit_ones(write_env):
    template = _synth(
        write_env,
        """
        name: uat
        cidr_block: 10.20.0.0/16
        azs: 2
        private_subnets: true
        public_subnet_cidrs: [10.20.0.0/24, 10.20.1.0/24]
        """,
    )

    cidrs = _cidrs(template)
    assert cidrs["int-production-use1-uat-public-primary"] == "10.20.0.0/24"
    assert cidrs["int-production-use1-uat-private-primary"] == "10.20.2.0/24"
    assert len(set(cidrs.values())) == 4


def test_cidr_mask_changes_the_carved_size(write_env):
    template = _synth(
        write_env,
        "name: uat\ncidr_block: 10.20.0.0/16\nazs: 2\ncidr_mask: 20",
    )

    assert set(_cidrs(template).values()) == {"10.20.0.0/20", "10.20.16.0/20"}


def test_common_tags_reach_every_resource(write_env):
    template = _synth(write_env, WITH_PRIVATE)

    expected = Match.array_with(
        [
            {"Key": "Build-method", "Value": "aws-cdk"},
            {"Key": "Compliance", "Value": "hippaa"},
            {"Key": "Environment", "Value": "test"},
            {"Key": "ManagedBy", "Value": "aws-cdk/repo"},
            {"Key": "Owner", "Value": "someone@example.com"},
        ]
    )
    for resource_type in (
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::InternetGateway",
        "AWS::EC2::NatGateway",
        "AWS::EC2::EIP",
        "AWS::EC2::RouteTable",
    ):
        template.has_resource_properties(resource_type, {"Tags": expected})


# --- the shipped environments ---------------------------------------------


@pytest.mark.parametrize("env_name", list_environments())
def test_shipped_environment_synthesizes_without_aws(env_name, tmp_path):
    """`cdk synth` runs before credentials exist in CI, so the assembly must
    not ask AWS for anything."""
    outdir = tmp_path / env_name
    _template(load_environment(env_name), outdir=outdir)

    manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
    assert not manifest.get("missing"), manifest["missing"]


def test_dev_is_public_only_and_prod_has_private_subnets():
    """The shipped configs demonstrate both shapes. Counts come from the config
    so editing azs in env.yaml does not break this."""
    dev_env = load_environment("dev")
    dev = _template(dev_env)
    assert dev_env.network.private_subnets is False
    dev.resource_count_is("AWS::EC2::Subnet", dev_env.network.azs)
    dev.resource_count_is("AWS::EC2::NatGateway", 0)
    dev.resource_count_is("AWS::EC2::EIP", 0)

    prod_env = load_environment("prod")
    prod = _template(prod_env)
    assert prod_env.network.private_subnets is True
    prod.resource_count_is("AWS::EC2::Subnet", prod_env.network.azs * 2)
    prod.resource_count_is("AWS::EC2::NatGateway", prod_env.network.nat_gateway_count)
    prod.resource_count_is("AWS::EC2::EIP", prod_env.network.nat_gateway_count)
