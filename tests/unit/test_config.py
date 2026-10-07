import textwrap

import pytest

from cognitech_cdk.common.config import (
    CommonProps,
    available_environments,
    load_environment,
)

VALID_TAGS = """
            Build-method: aws-cdk
            Environment: user-acceptance-test
            ManagedBy: aws-cdk/repo
            Owner: kbrigthain@gmail.com
            Compliance: hippaa
"""


def _write_env(root, name: str, body: str) -> None:
    env_dir = root / name
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "env.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def test_resource_name_matches_terraform_convention():
    common = CommonProps(
        account_name="int-production",
        region_prefix="use1",
        tags={},
    )
    assert common.resource_name("uat", "vpc") == "int-production-use1-uat-vpc"
    assert common.resource_name("uat", "igw") == "int-production-use1-uat-igw"
    assert (
        common.resource_name("uat", "app", "private", "primary")
        == "int-production-use1-uat-app-private-primary"
    )


def test_environments_are_discovered_in_promotion_order():
    assert available_environments() == ["uat", "prod"]


def test_every_environment_declares_the_required_tags():
    for env_name in available_environments():
        tags = load_environment(env_name).common.tags
        assert {"Build-method", "Environment", "ManagedBy", "Owner", "Compliance"} <= tags.keys()


def test_name_is_rejected_in_common_tags(tmp_path):
    _write_env(
        tmp_path,
        "bad",
        f"""
        account_id: "111122223333"
        region: us-east-1
        common:
          account_name: int-production
          region_prefix: use1
          tags:
            Name: int-production-use1-uat-vpc{VALID_TAGS}
        networks: []
        """,
    )
    with pytest.raises(ValueError, match="'Name' must not be set"):
        load_environment("bad", deployments_dir=tmp_path)


def test_missing_required_tag_is_rejected(tmp_path):
    _write_env(
        tmp_path,
        "bad",
        """
        account_id: "111122223333"
        region: us-east-1
        common:
          account_name: int-production
          region_prefix: use1
          tags:
            Build-method: aws-cdk
        networks: []
        """,
    )
    with pytest.raises(ValueError, match="missing required keys"):
        load_environment("bad", deployments_dir=tmp_path)


def test_unknown_environment_lists_the_known_ones(tmp_path):
    _write_env(
        tmp_path,
        "uat",
        f"""
        account_id: "111122223333"
        region: us-east-1
        common:
          account_name: int-production
          region_prefix: use1
          tags:{VALID_TAGS}
        networks: []
        """,
    )
    with pytest.raises(FileNotFoundError, match="Known: uat"):
        load_environment("staging", deployments_dir=tmp_path)
