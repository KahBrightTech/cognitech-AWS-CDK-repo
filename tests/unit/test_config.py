import textwrap

import pytest

from cognitech_cdk.common.config import (
    CommonProps,
    available_environments,
    load_environment,
)

HEADER = """account_id: "111122223333"
region: us-east-1
common:
  account_name: int-production
  region_prefix: use1
  qualifier: cdk
  tags:
    Build-method: aws-cdk
    Environment: user-acceptance-test
    ManagedBy: aws-cdk/repo
    Owner: kbrigthain@gmail.com
    Compliance: hippaa
networks:
"""


def _write_env(root, name: str, networks: str):
    env_dir = root / name
    env_dir.mkdir(parents=True, exist_ok=True)
    body = textwrap.indent(textwrap.dedent(networks).strip(), "  ")
    (env_dir / "env.yaml").write_text(f"{HEADER}{body}\n", encoding="utf-8")
    return root


def _load(tmp_path, networks: str):
    root = _write_env(tmp_path, "test", networks)
    return load_environment("test", deployments_dir=root)


def test_resource_name_skips_empty_segments():
    assert (
        CommonProps("int-production", "use1", {}).resource_name("uat", "vpc")
        == "int-production-use1-uat-vpc"
    )
    assert (
        CommonProps("int-production", "use1", {}, qualifier="cdk").resource_name("uat", "vpc")
        == "int-production-use1-cdk-uat-vpc"
    )


def test_environments_are_discovered_in_promotion_order():
    assert available_environments() == ["uat", "prod"]


def test_every_environment_declares_the_required_tags():
    for env_name in available_environments():
        tags = load_environment(env_name).common.tags
        assert {"Build-method", "Environment", "ManagedBy", "Owner", "Compliance"} <= tags.keys()


def test_explicit_cidrs_are_used_verbatim(tmp_path):
    config = _load(
        tmp_path,
        """
        - name: uat
          cidr_block: 10.20.0.0/16
          availability_zones: [us-east-1a, us-east-1b]
          nat_gateways: 0
          subnet_groups:
            - name: edge
              type: public
              cidrs: [10.20.8.0/22, 10.20.12.0/22]
        """,
    )
    group = config.networks[0].subnet_groups[0]
    assert group.cidrs == ("10.20.8.0/22", "10.20.12.0/22")


def test_cidr_mask_is_carved_from_the_vpc_range(tmp_path):
    config = _load(
        tmp_path,
        """
        - name: uat
          cidr_block: 10.20.0.0/16
          az_count: 2
          nat_gateways: 0
          subnet_groups:
            - name: edge
              type: public
              cidr_mask: 20
            - name: data
              type: isolated
              cidr_mask: 22
        """,
    )
    network = config.networks[0]
    assert network.availability_zones == ("us-east-1a", "us-east-1b")
    assert network.subnet_groups[0].cidrs == ("10.20.0.0/20", "10.20.16.0/20")
    assert network.subnet_groups[1].cidrs == ("10.20.32.0/22", "10.20.36.0/22")


def test_explicit_cidrs_are_reserved_before_carving(tmp_path):
    config = _load(
        tmp_path,
        """
        - name: uat
          cidr_block: 10.20.0.0/16
          az_count: 1
          nat_gateways: 0
          subnet_groups:
            - name: edge
              type: public
              cidr_mask: 20
            - name: data
              type: isolated
              cidrs: [10.20.0.0/20]
        """,
    )
    assert config.networks[0].subnet_groups[0].cidrs == ("10.20.16.0/20",)


def test_public_only_network_needs_no_nat(tmp_path):
    config = _load(
        tmp_path,
        """
        - name: dmz
          cidr_block: 10.21.0.0/16
          az_count: 2
          subnet_groups:
            - name: edge
              type: public
              cidr_mask: 20
        """,
    )
    assert config.networks[0].nat_gateways == 0


@pytest.mark.parametrize(
    "networks, message",
    [
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              az_count: 2
              nat_gateways: 0
              subnet_groups:
                - name: app
                  type: private
                  cidr_mask: 20
            """,
            "need nat_gateways >= 1",
        ),
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              az_count: 2
              nat_gateways: 1
              subnet_groups:
                - name: app
                  type: private
                  cidr_mask: 20
            """,
            "needs a 'public' subnet group",
        ),
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              az_count: 2
              nat_gateways: 1
              subnet_groups:
                - name: edge
                  type: public
                  cidr_mask: 20
            """,
            "no 'private' subnet group",
        ),
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              availability_zones: [us-east-1a, us-east-1b]
              nat_gateways: 0
              subnet_groups:
                - name: edge
                  type: public
                  cidrs: [10.20.0.0/20]
            """,
            "lists 1 CIDRs but the network uses 2",
        ),
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              az_count: 1
              nat_gateways: 0
              subnet_groups:
                - name: edge
                  type: public
                  cidrs: [10.99.0.0/20]
            """,
            "outside the VPC range",
        ),
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              az_count: 1
              nat_gateways: 0
              subnet_groups:
                - name: edge
                  type: public
                  cidrs: [10.20.0.0/20]
                - name: data
                  type: isolated
                  cidrs: [10.20.4.0/22]
            """,
            "overlap",
        ),
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              az_count: 1
              nat_gateways: 0
              subnet_groups:
                - name: edge
                  type: public
                  cidrs: [10.20.0.0/20]
                  cidr_mask: 20
            """,
            "pick one",
        ),
        (
            """
            - name: uat
              cidr_block: 10.20.0.0/16
              nat_gateways: 0
              subnet_groups:
                - name: edge
                  type: public
                  cidr_mask: 20
            """,
            "availability_zones or az_count",
        ),
    ],
)
def test_invalid_network_is_rejected(tmp_path, networks, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, networks)


def test_name_is_rejected_in_common_tags(tmp_path):
    env_dir = tmp_path / "bad"
    env_dir.mkdir()
    (env_dir / "env.yaml").write_text(
        HEADER.replace("  tags:\n", "  tags:\n    Name: oops\n") + "  []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'Name' must not be set"):
        load_environment("bad", deployments_dir=tmp_path)


def test_unknown_environment_lists_the_known_ones(tmp_path):
    root = _write_env(tmp_path, "uat", "[]")
    with pytest.raises(FileNotFoundError, match="Known: uat"):
        load_environment("staging", deployments_dir=root)
