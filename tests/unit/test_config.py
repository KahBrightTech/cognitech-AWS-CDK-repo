import pytest
import yaml

from cognitech_cdk.common.config import Common
from cognitech_cdk.common.environment import (
    list_environments, load_environment, load_environment_config, resolve_environment,
)
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
    assert list_environments() == ["md-preprod/dev", "prod"]


@pytest.mark.parametrize("buckets", ["[]", "null"])
def test_grouped_defaults_and_environment_overrides(write_env, buckets):
    root = write_env(
        "name: base\ncidr_block: 10.0.0.0/16\nazs: 2\n"
        "public_subnet_cidrs: [10.0.0.0/24, 10.0.1.0/24]",
        name="prod",
    )
    account = root / "example"
    (account / "dev").mkdir(parents=True)
    (account / "base.yaml").write_text(
        (root / "prod/env.yaml").read_text() + "s3_buckets:\n- name: inherited\n",
        encoding="utf-8",
    )
    (account / "dev/env.yaml").write_text(
        'account_id: "999900001111"\nregion: us-west-2\n'
        "common:\n  global: true\n  environment_abr: dev\n"
        "  region_prefix: usw2\n  tags:\n    Environment: development\n"
        "network:\n  name: dev\n  azs: 1\n  public_subnet_cidrs: [10.0.2.0/24]\n"
        f"s3_buckets: {buckets}\n",
        encoding="utf-8",
    )
    environment = load_environment("example/dev", root=root)
    assert environment == load_environment("dev", root=root)
    assert environment.name == "dev"
    assert environment.account_id == "999900001111"
    assert environment.region == "us-west-2"
    assert environment.common.global_ is True
    assert environment.common.account_name == "int-production"
    assert environment.common.region_prefix == "usw2"
    assert environment.common.environment_abr == "dev"
    assert environment.common.tags["Environment"] == "development"
    assert environment.common.tags["ManagedBy"] == "aws-cdk/repo"
    assert environment.network.name == "dev"
    assert environment.network.cidr_block == "10.0.0.0/16"
    assert environment.network.public_subnet_cidrs == ("10.0.2.0/24",)
    assert not environment.storage
    assert load_environment("prod", root=root).region == "us-east-1"
    assert list_environments(root) == ["example/dev", "prod"]


def test_migrated_dev_preserves_legacy_selection_and_defaults():
    environment = load_environment("md-preprod/dev")
    assert environment == load_environment("dev")
    assert environment.name == "dev"
    assert environment.account_id == "533267408704"
    assert environment.region == "us-east-1"
    assert environment.common.account_name == "md-preproduction"
    assert environment.common.account_name_abr == "mdpreprod"
    assert environment.common.environment_abr == "dev"
    assert environment.common.tags["Compliance"] == "FedRAMP"


@pytest.mark.parametrize("selected", ["test", "example/qa"])
def test_resource_placeholders_use_effective_account_and_environment(write_env, selected):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name=selected)
    path = root / selected / "env.yaml"
    original = path.read_text()
    if "/" in selected:
        (root / "example/base.yaml").write_text(original, encoding="utf-8")
        original = (
            'account_id: "999900001111"\nregion: us-west-2\n'
            "common:\n  account_name: other-account\n  region_prefix: usw2\n"
        )
    path.write_text(original + """
key_pairs:
- name: &node_key "[[environment]]-ssh"
  create_secret: true
  secret_name: "[[account_name_abr]]-[[environment]]-private-key"
  secret_description: "Key for [[account_name]] [[environment]] in [[region_prefix]] ([[region]] / [[account_id]])"
eks:
- name: main
  compute:
    launch_templates:
    - name: general
      key_pair: *node_key
""", encoding="utf-8")
    environment = load_environment(selected, root=root)
    key = environment.key_pairs.key_pairs[0]
    assert key.name == f"{environment.name}-ssh"
    assert key.secret_name == f"intprod-{environment.name}-private-key"
    assert key.secret_description == (
        f"Key for {environment.common.account_name} {environment.name} in "
        f"{environment.common.region_prefix} ({environment.region} / {environment.account_id})"
    )
    assert environment.eks.clusters[0].launch_templates[0].key_pair == key.name


def test_unknown_resource_placeholder_fails_with_field_context(write_env):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16")
    path = root / "test/env.yaml"
    path.write_text(path.read_text() + """
key_pairs:
- name: ssh
  create_secret: true
  secret_description: "Key for [[acount_name]]"
""", encoding="utf-8")
    with pytest.raises(ValueError, match=r"key_pairs\[0\].secret_description: unresolved placeholders: acount_name"):
        load_environment("test", root=root)


def test_deployment_placeholders_render_arns_lists_and_embedded_templates(write_env):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16")
    path = root / "test/env.yaml"
    path.write_text(path.read_text() + """
examples:
  role_arn: "arn:aws:iam::[[account_id]]:role/deploy-[[environment]]"
  hosts: ["argocd.[[environment]].example.com"]
  node_class_manifest: "policies/[[account_name]]-[[environment]].yaml"
  policy:
    Resource: ["arn:aws:s3:::[[account_name]]", "[[bucket_arn]]"]
  values:
    description: "[[account_name]] [[chart_variable]]"
  node_pool_manifest:
    metadata:
      name: "[[account_name]]-[[custom_pool]]"
""", encoding="utf-8")
    raw = load_environment_config("test", root=root)
    examples = raw["examples"]
    assert examples["role_arn"] == f"arn:aws:iam::{raw['account_id']}:role/deploy-test"
    assert examples["hosts"] == ["argocd.test.example.com"]
    assert examples["node_class_manifest"] == "policies/int-production-test.yaml"
    assert examples["policy"]["Resource"] == [
        "arn:aws:s3:::int-production", "[[bucket_arn]]",
    ]
    assert examples["values"]["description"] == "int-production [[chart_variable]]"
    assert examples["node_pool_manifest"]["metadata"]["name"] == "int-production-[[custom_pool]]"


@pytest.mark.parametrize("override", [None, "other.example.org"])
def test_account_domain_renders_certificates_issuer_and_hosts(write_env, override):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    path = root / "example/dev/env.yaml"
    base = yaml.safe_load(path.read_text())
    base["common"]["domain_name"] = "example.com"
    (root / "example/base.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    path.write_text(yaml.safe_dump({
        **({"common": {"domain_name": override}} if override is not None else {}),
        "certificates": [{
            "name": "wildcard", "domain_name": "*.[[environment]].[[domain_name]]",
            "zone_name": "[[domain_name]]",
        }],
        "eks_addons": [{
            "cluster": "main",
            "addons": [
                {"name": "eks-pod-identity-agent"},
                {"name": "cert-manager", "pod_identity": True, "cluster_issuer": {
                    "name": "[[environment]]", "dns_zone": "[[domain_name]]",
                }},
            ],
            "aws_load_balancer_controller": {
                "enabled": True,
                "policy": {"Statement": [{
                    "Effect": "Allow", "Action": "elasticloadbalancing:DescribeLoadBalancers", "Resource": "*",
                }]},
            },
            **{
                app: {"host": f"{host}.[[environment]].[[domain_name]]"}
                for app, host in (("argocd", "argocd"), ("kubecost", "kubecost"), ("awx_operator", "awx"))
            },
        }],
    }), encoding="utf-8")
    environment = load_environment("example/dev", root=root)
    domain = override or "example.com"
    assert environment.common.domain_name == domain
    certificate = environment.certificates.certificates[0]
    assert certificate.domain_name == f"*.dev.{domain}"
    assert certificate.zone_name == domain
    addons = environment.eks_addons.clusters[0]
    cert_manager = next(item for item in addons.addons if item.name == "cert-manager")
    assert cert_manager.cluster_issuer.dns_zone == domain
    assert addons.argocd.host == f"argocd.dev.{domain}"
    assert addons.kubecost.host == f"kubecost.dev.{domain}"
    assert addons.awx_operator.host == f"awx.dev.{domain}"


def test_domain_placeholder_requires_an_account_domain(write_env):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16")
    path = root / "test/env.yaml"
    path.write_text(path.read_text() + """
certificates:
- name: wildcard
  domain_name: "*.[[environment]].[[domain_name]]"
  zone_name: "[[domain_name]]"
""", encoding="utf-8")
    with pytest.raises(ValueError, match=r"common.domain_name is required when using \[\[domain_name\]\]"):
        load_environment("test", root=root)


@pytest.mark.parametrize("field", ["account_id", "region", "network"])
def test_required_defaults_cannot_be_cleared_by_null(write_env, field):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    path = root / "example/dev/env.yaml"
    (root / "example/base.yaml").write_text(path.read_text(), encoding="utf-8")
    path.write_text(f"{field}: null\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"missing required fields after merging: {field}"):
        load_environment("example/dev", root=root)


def test_account_principal_aliases_keep_policies_and_scopes_in_the_environment(write_env):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    path = root / "example/dev/env.yaml"
    principals = {
        "admin": "arn:aws:iam::111122223333:role/admin",
        "unused": "arn:aws:iam::111122223333:role/unused",
    }
    (root / "example/base.yaml").write_text(
        path.read_text() + yaml.safe_dump({"eks_access_principals": principals}), encoding="utf-8",
    )
    path.write_text(yaml.safe_dump({"eks": [
        {"name": "main", "access_entries": {"admin": {
            "principal_arns": ["admin"],
            "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy",
            "namespaces": ["infogrid", "healthpath"], "kubernetes_groups": ["app-admins"],
        }}},
        {"name": "isolated"},
    ]}), encoding="utf-8")
    environment = load_environment("example/dev", root=root)
    entries = environment.eks.clusters[0].access_entries
    assert len(entries) == 1
    assert entries[0].principal_arns == ("arn:aws:iam::111122223333:role/admin",)
    assert entries[0].policy_arns == ("arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy",)
    assert entries[0].namespaces == ("infogrid", "healthpath")
    assert entries[0].kubernetes_groups == ("app-admins",)
    assert environment.eks.clusters[1].access_entries == ()


@pytest.mark.parametrize("references", [
    ["viewer"], ["arn:aws:iam::111122223333:role/viewer"],
    ["viewer", "arn:aws:iam::111122223333:user/auditor"],
])
def test_principal_aliases_preserve_literal_arns_and_mixed_lists(write_env, references):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    path = root / "example/dev/env.yaml"
    principals = {"viewer": "arn:aws:iam::111122223333:role/viewer"}
    (root / "example/base.yaml").write_text(
        path.read_text() + yaml.safe_dump({"eks_access_principals": principals}), encoding="utf-8",
    )
    path.write_text(yaml.safe_dump({"eks": [{"name": "main", "access_entries": {"admin": {
        "principal_arns": references,
        "policy_arn": "AmazonEKSViewPolicy",
        "namespaces": ["infogrid"],
    }}}]}), encoding="utf-8")
    entry = load_environment("example/dev", root=root).eks.clusters[0].access_entries[0]
    assert entry.principal_arns == tuple(principals.get(reference, reference) for reference in references)
    assert entry.policy_arns == ("AmazonEKSViewPolicy",)
    assert entry.namespaces == ("infogrid",)


@pytest.mark.parametrize("principals", [[], {"admin": []}, {"admin": None}, {"admin": "not-an-arn"}])
def test_invalid_account_principal_aliases_are_rejected(write_env, principals):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    path = root / "example/base.yaml"
    path.write_text(yaml.safe_dump({"eks_access_principals": principals}), encoding="utf-8")
    with pytest.raises(ValueError, match="eks_access_principals"):
        load_environment("example/dev", root=root)


def test_principal_alias_can_be_overridden_and_unknown_references_fail(write_env):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    path = root / "example/dev/env.yaml"
    (root / "example/base.yaml").write_text(
        path.read_text() + yaml.safe_dump({"eks_access_principals": {
            "admin": "arn:aws:iam::111122223333:role/admin",
        }}), encoding="utf-8",
    )
    configuration = {
        "eks_access_principals": {"admin": "arn:aws:iam::111122223333:role/environment-admin"},
        "eks": [{"name": "main", "access_entries": {"admin": {
            "principal_arns": ["admin"], "policy_arn": "AmazonEKSAdminPolicy",
        }}}],
    }
    path.write_text(yaml.safe_dump(configuration), encoding="utf-8")
    entry = load_environment("example/dev", root=root).eks.clusters[0].access_entries[0]
    assert entry.principal_arns == ("arn:aws:iam::111122223333:role/environment-admin",)
    configuration["eks"][0]["access_entries"]["admin"]["principal_arns"] = ["missing"]
    path.write_text(yaml.safe_dump(configuration), encoding="utf-8")
    with pytest.raises(ValueError, match="eks_access_principals reference"):
        load_environment("example/dev", root=root)


@pytest.mark.parametrize("other_name", ["dev", "other/dev"])
def test_ambiguous_short_names_require_qualified_selection(write_env, other_name):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    write_env("name: n\ncidr_block: 10.0.0.0/16", name=other_name)
    with pytest.raises(ValueError, match="Ambiguous environment 'dev'"):
        load_environment("dev", root=root)
    assert resolve_environment("example/dev", root=root) == "example/dev"
    assert load_environment("example/dev", root=root).name == "dev"


@pytest.mark.parametrize("selector", ["../prod", "/prod", "example/../prod"])
def test_environment_selectors_cannot_traverse_directories(write_env, selector):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="prod")
    with pytest.raises(ValueError, match="Environment"):
        load_environment(selector, root=root)


@pytest.mark.parametrize("filename", ["base.yaml", "dev/env.yaml"])
@pytest.mark.parametrize("content", ["[]", "common: [", "1: value"])
def test_invalid_grouped_yaml_reports_its_file(write_env, filename, content):
    root = write_env("name: n\ncidr_block: 10.0.0.0/16", name="example/dev")
    path = root / "example" / filename
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=filename):
        load_environment("example/dev", root=root)


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
