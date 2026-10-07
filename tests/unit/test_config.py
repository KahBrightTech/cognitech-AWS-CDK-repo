import pytest

from cognitech_cdk.common.config import Common
from cognitech_cdk.common.environment import list_environments, load_environment
from cognitech_cdk.constructs.network import NetworkProps


def _load(write_env, network: str):
    return load_environment("test", root=write_env(network))


# --- naming convention ----------------------------------------------------


def test_name_matches_the_terraform_convention():
    common = Common(account_name="int-production", region_prefix="use1", tags={})

    # {account_name}-{region_prefix}-{vpc_name}-vpc
    assert common.name("uat", "vpc") == "int-production-use1-uat-vpc"
    assert common.name("uat", "igw") == "int-production-use1-uat-igw"
    # {account_name}-{region_prefix}-{vpc_name}-{subnet_name}-{type}-{ordinal}
    assert (
        common.name("uat", "app", "private", "primary")
        == "int-production-use1-uat-app-private-primary"
    )


def test_name_skips_empty_parts():
    common = Common(account_name="acme", region_prefix="use1", tags={})
    assert common.name("vpc", "", "igw") == "acme-use1-vpc-igw"


def test_abbreviated_name_uses_the_short_account_name():
    common = Common(
        account_name="int-production", region_prefix="use1", tags={}, account_name_abr="intprod"
    )
    assert common.abbreviated_name("uat", "alb") == "intprod-use1-uat-alb"


def test_abbreviated_name_falls_back_to_the_full_name():
    common = Common(account_name="int-production", region_prefix="use1", tags={})
    assert common.abbreviated_name("uat", "alb") == "int-production-use1-uat-alb"


def test_common_carries_the_terraform_fields(write_env):
    common = _load(write_env, "name: t\ncidr_block: 10.0.0.0/16").common
    assert common.global_ is False
    assert common.account_name == "int-production"
    assert common.region_prefix == "use1"
    assert common.account_name_abr == "intprod"
    assert common.environment_abr == "tst"


# --- validation -----------------------------------------------------------


def test_name_is_rejected_as_a_common_tag(tmp_path):
    env_dir = tmp_path / "bad"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        'account_id: "1"\nregion: us-east-1\n'
        "common:\n  account_name: a\n  region_prefix: use1\n  tags:\n    Name: oops\n"
        "network:\n  name: n\n  cidr_block: 10.0.0.0/16\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'Name' must not be a common tag"):
        load_environment("bad", root=tmp_path)


def test_missing_required_tag_is_rejected(tmp_path):
    env_dir = tmp_path / "bad"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        'account_id: "1"\nregion: us-east-1\n'
        "common:\n  account_name: a\n  region_prefix: use1\n"
        "  tags:\n    Owner: someone@example.com\n"
        "network:\n  name: n\n  cidr_block: 10.0.0.0/16\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing Build-method"):
        load_environment("bad", root=tmp_path)


def test_unknown_environment_lists_the_known_ones(write_env):
    root = write_env("name: t\ncidr_block: 10.0.0.0/16", name="dev")
    with pytest.raises(FileNotFoundError, match="Known environments: dev"):
        load_environment("nope", root=root)


def test_environments_are_discovered():
    assert list_environments() == ["dev", "prod"]


# --- network props --------------------------------------------------------


@pytest.mark.parametrize("azs", [1, 2, 3, 4])
def test_azs_between_one_and_four_are_allowed(azs):
    assert NetworkProps(name="n", cidr_block="10.0.0.0/16", azs=azs).azs == azs


@pytest.mark.parametrize("azs", [0, 5])
def test_azs_outside_the_range_are_rejected(azs):
    with pytest.raises(ValueError, match="azs must be between 1 and 4"):
        NetworkProps(name="n", cidr_block="10.0.0.0/16", azs=azs)


def test_public_only_network_has_no_nat_gateways():
    props = NetworkProps(name="n", cidr_block="10.0.0.0/16", azs=3)
    assert props.private_subnets is False
    assert props.nat_gateway_count == 0


def test_nat_gateways_default_to_one_per_az():
    props = NetworkProps(name="n", cidr_block="10.0.0.0/16", azs=3, private_subnets=True)
    assert props.nat_gateway_count == 3


def test_nat_gateways_can_be_reduced():
    props = NetworkProps(
        name="n", cidr_block="10.0.0.0/16", azs=3, private_subnets=True, nat_gateways=1
    )
    assert props.nat_gateway_count == 1


def test_nat_gateways_are_capped_at_the_az_count():
    props = NetworkProps(
        name="n", cidr_block="10.0.0.0/16", azs=2, private_subnets=True, nat_gateways=9
    )
    assert props.nat_gateway_count == 2


def test_private_subnets_without_a_nat_gateway_are_rejected():
    with pytest.raises(ValueError, match="need at least one NAT gateway"):
        NetworkProps(
            name="n", cidr_block="10.0.0.0/16", azs=2, private_subnets=True, nat_gateways=0
        )


def test_network_props_are_read_from_yaml(write_env):
    network = _load(
        write_env,
        """
        name: uat
        cidr_block: 10.20.0.0/16
        azs: 3
        private_subnets: true
        nat_gateways: 1
        public_subnet_name: edge
        private_subnet_name: app
        cidr_mask: 20
        """,
    ).network

    assert network.name == "uat"
    assert network.azs == 3
    assert network.private_subnets is True
    assert network.nat_gateway_count == 1
    assert network.public_subnet_name == "edge"
    assert network.private_subnet_name == "app"
    assert network.cidr_mask == 20


# --- subnet CIDRs ---------------------------------------------------------


def test_cidrs_are_carved_when_not_given():
    props = NetworkProps(name="n", cidr_block="10.20.0.0/16", azs=2, private_subnets=True)
    public, private = props.subnet_cidrs()

    assert public == ("10.20.0.0/24", "10.20.1.0/24")
    assert private == ("10.20.2.0/24", "10.20.3.0/24")


def test_cidr_mask_controls_the_carved_size():
    props = NetworkProps(name="n", cidr_block="10.20.0.0/16", azs=2, cidr_mask=20)
    public, _ = props.subnet_cidrs()

    assert public == ("10.20.0.0/20", "10.20.16.0/20")


def test_explicit_cidrs_are_used_verbatim():
    props = NetworkProps(
        name="n",
        cidr_block="10.20.0.0/16",
        azs=2,
        private_subnets=True,
        public_subnet_cidrs=["10.20.1.0/24", "10.20.2.0/24"],
        private_subnet_cidrs=["10.20.50.0/23", "10.20.52.0/23"],
    )
    public, private = props.subnet_cidrs()

    assert public == ("10.20.1.0/24", "10.20.2.0/24")
    assert private == ("10.20.50.0/23", "10.20.52.0/23")


def test_explicit_cidrs_are_reserved_before_carving():
    """A tier without CIDRs must not be given a range already taken."""
    props = NetworkProps(
        name="n",
        cidr_block="10.20.0.0/16",
        azs=2,
        private_subnets=True,
        public_subnet_cidrs=["10.20.0.0/24", "10.20.1.0/24"],
    )
    public, private = props.subnet_cidrs()

    assert public == ("10.20.0.0/24", "10.20.1.0/24")
    assert private == ("10.20.2.0/24", "10.20.3.0/24")
    assert not set(public) & set(private)


def test_public_only_network_carves_no_private_cidrs():
    props = NetworkProps(name="n", cidr_block="10.20.0.0/16", azs=2)
    public, private = props.subnet_cidrs()

    assert len(public) == 2
    assert private == ()


def test_cidrs_are_read_from_yaml(write_env):
    network = _load(
        write_env,
        """
        name: uat
        cidr_block: 10.20.0.0/16
        azs: 2
        private_subnets: true
        public_subnet_cidrs: [10.20.8.0/22, 10.20.12.0/22]
        private_subnet_cidrs: [10.20.16.0/22, 10.20.20.0/22]
        """,
    ).network

    assert network.subnet_cidrs() == (
        ("10.20.8.0/22", "10.20.12.0/22"),
        ("10.20.16.0/22", "10.20.20.0/22"),
    )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (
            {"azs": 3, "public_subnet_cidrs": ["10.20.0.0/24", "10.20.1.0/24"]},
            "lists 2 CIDRs but azs is 3",
        ),
        (
            {"azs": 1, "public_subnet_cidrs": ["10.99.0.0/24"]},
            "outside the VPC range",
        ),
        (
            {
                "azs": 1,
                "private_subnets": True,
                "public_subnet_cidrs": ["10.20.0.0/24"],
                "private_subnet_cidrs": ["10.20.0.0/25"],
            },
            "overlap",
        ),
    ],
)
def test_invalid_cidrs_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        NetworkProps(name="n", cidr_block="10.20.0.0/16", **kwargs)


def test_running_out_of_space_is_reported():
    # A /24 VPC split into /26 gives 4 blocks; 3 AZs x 2 tiers needs 6.
    with pytest.raises(ValueError, match="no free /26 block left"):
        NetworkProps(
            name="n",
            cidr_block="10.20.0.0/24",
            azs=3,
            private_subnets=True,
            cidr_mask=26,
        )
