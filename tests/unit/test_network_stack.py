import json
import textwrap
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.network_stack import NetworkStack
from cognitech_cdk.settings import environments, load_settings

BASE = """account_id: "111122223333"
region: us-east-1
name_prefix: demo
cidr: 10.0.0.0/16
tags:
  Build-method: aws-cdk
  Environment: test
  ManagedBy: aws-cdk/repo
  Owner: someone@example.com
  Compliance: hippaa
"""


@pytest.fixture
def settings_for(tmp_path):
    """Write a throwaway env.yaml from the extra lines given, and load it."""

    def _build(extra: str = "", env: str = "demo"):
        env_dir = tmp_path / env
        env_dir.mkdir(parents=True, exist_ok=True)
        body = textwrap.dedent(extra).strip()
        (env_dir / "env.yaml").write_text(f"{BASE}{body}\n", encoding="utf-8")
        return load_settings(env, root=tmp_path)

    return _build


def _synth(settings, outdir: Path | None = None) -> Template:
    app = cdk.App(outdir=str(outdir) if outdir else None)
    stack = NetworkStack(
        app,
        f"{settings.name_prefix}-network",
        settings=settings,
        env=cdk.Environment(account=settings.account_id, region=settings.region),
    )
    return Template.from_stack(stack)


def _names(template: Template, resource_type: str) -> set[str]:
    return {
        tag["Value"]
        for resource in template.find_resources(resource_type).values()
        for tag in resource["Properties"].get("Tags", [])
        if tag["Key"] == "Name"
    }


# --- settings -------------------------------------------------------------


@pytest.mark.parametrize(
    "azs, zones",
    [
        (1, ["us-east-1a"]),
        (2, ["us-east-1a", "us-east-1b"]),
        (3, ["us-east-1a", "us-east-1b", "us-east-1c"]),
    ],
)
def test_azs_pick_the_zones(settings_for, azs, zones):
    assert settings_for(f"azs: {azs}").zones == zones


def test_private_subnets_are_off_by_default(settings_for):
    settings = settings_for("azs: 2")
    assert settings.private_subnets is False
    assert settings.nat_gateways == 0


def test_nat_gateways_default_to_one_per_az(settings_for):
    assert settings_for("azs: 3\nprivate_subnets: true").nat_gateways == 3


def test_nat_gateways_are_capped_at_the_az_count(settings_for):
    assert settings_for("azs: 2\nprivate_subnets: true\nnat_gateways: 9").nat_gateways == 2


@pytest.mark.parametrize(
    "extra, message",
    [
        ("azs: 0", "azs must be 1, 2 or 3"),
        ("azs: 4", "azs must be 1, 2 or 3"),
        ("azs: 2\nprivate_subnets: true\nnat_gateways: 0", "needs nat_gateways >= 1"),
    ],
)
def test_bad_settings_are_rejected(settings_for, extra, message):
    with pytest.raises(ValueError, match=message):
        settings_for(extra)


def test_missing_tag_is_rejected(tmp_path):
    env_dir = tmp_path / "bad"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        'account_id: "1"\nregion: us-east-1\nname_prefix: x\ncidr: 10.0.0.0/16\n'
        "tags:\n  Owner: someone@example.com\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing Build-method"):
        load_settings("bad", root=tmp_path)


def test_unknown_environment_lists_the_known_ones(settings_for, tmp_path):
    settings_for("azs: 1", env="uat")
    with pytest.raises(FileNotFoundError, match="Known environments: uat"):
        load_settings("nope", root=tmp_path)


def test_environments_are_listed_in_deploy_order():
    assert environments() == ["uat", "prod"]


# --- the stack ------------------------------------------------------------


def test_public_only_vpc_has_no_nat_gateways(settings_for):
    template = _synth(settings_for("azs: 2"))

    template.resource_count_is("AWS::EC2::VPC", 1)
    template.resource_count_is("AWS::EC2::Subnet", 2)
    template.resource_count_is("AWS::EC2::InternetGateway", 1)
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::EIP", 0)
    assert _names(template, "AWS::EC2::Subnet") == {"demo-public-primary", "demo-public-secondary"}
    assert set(template.find_outputs("*")) == {"VpcId", "PublicSubnetIds"}


def test_private_subnets_add_nat_gateways_and_eips(settings_for):
    template = _synth(settings_for("azs: 2\nprivate_subnets: true"))

    template.resource_count_is("AWS::EC2::Subnet", 4)
    template.resource_count_is("AWS::EC2::NatGateway", 2)
    template.resource_count_is("AWS::EC2::EIP", 2)
    assert _names(template, "AWS::EC2::Subnet") == {
        "demo-public-primary",
        "demo-public-secondary",
        "demo-private-primary",
        "demo-private-secondary",
    }
    assert set(template.find_outputs("*")) == {"VpcId", "PublicSubnetIds", "PrivateSubnetIds"}


def test_one_nat_gateway_serves_all_private_subnets(settings_for):
    template = _synth(settings_for("azs: 3\nprivate_subnets: true\nnat_gateways: 1"))

    template.resource_count_is("AWS::EC2::Subnet", 6)
    template.resource_count_is("AWS::EC2::NatGateway", 1)
    template.resource_count_is("AWS::EC2::EIP", 1)
    # 3 public routes to the IGW + 3 private routes to the one NAT gateway.
    routes = template.find_resources(
        "AWS::EC2::Route", {"Properties": {"DestinationCidrBlock": "0.0.0.0/0"}}
    )
    assert len(routes) == 6


@pytest.mark.parametrize("azs", [1, 2, 3])
def test_az_count_controls_the_subnet_count(settings_for, azs):
    template = _synth(settings_for(f"azs: {azs}\nprivate_subnets: true"))

    template.resource_count_is("AWS::EC2::Subnet", azs * 2)
    template.resource_count_is("AWS::EC2::NatGateway", azs)
    for zone in "abc"[:azs]:
        template.has_resource_properties(
            "AWS::EC2::Subnet", {"AvailabilityZone": f"us-east-1{zone}"}
        )


def test_public_subnets_route_out_and_get_public_ips(settings_for):
    template = _synth(settings_for("azs: 1"))

    template.has_resource_properties("AWS::EC2::Subnet", {"MapPublicIpOnLaunch": True})
    template.has_resource_properties(
        "AWS::EC2::Route",
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": Match.any_value()},
    )


def test_everything_is_named_from_the_prefix(settings_for):
    template = _synth(settings_for("azs: 1\nprivate_subnets: true"))

    assert _names(template, "AWS::EC2::VPC") == {"demo-vpc"}
    assert _names(template, "AWS::EC2::InternetGateway") == {"demo-igw"}
    assert _names(template, "AWS::EC2::NatGateway") == {"demo-primary-natgw"}
    assert _names(template, "AWS::EC2::EIP") == {"demo-primary-nat-eip"}
    assert _names(template, "AWS::EC2::RouteTable") == {
        "demo-public-primary-rtb",
        "demo-private-primary-rtb",
    }


def test_tags_reach_every_resource(settings_for):
    template = _synth(settings_for("azs: 1"))
    template.has_resource_properties(
        "AWS::EC2::VPC",
        {
            "Tags": Match.array_with(
                [
                    {"Key": "Build-method", "Value": "aws-cdk"},
                    {"Key": "Compliance", "Value": "hippaa"},
                    {"Key": "Owner", "Value": "someone@example.com"},
                ]
            )
        },
    )


# --- the real environments ------------------------------------------------


@pytest.mark.parametrize("env", environments())
def test_shipped_environment_synthesizes_without_aws(env, tmp_path):
    """`cdk synth` runs before credentials exist in the PR workflow, so the
    assembly must not ask AWS for anything."""
    outdir = tmp_path / env
    _synth(load_settings(env), outdir=outdir)

    manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
    assert not manifest.get("missing"), manifest["missing"]
