"""IAM trust, permissions, profiles, names, exports and config validation."""

import json
from dataclasses import replace

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.iam import IamProps
from cognitech_cdk.stacks.iam_stack import IamStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16"
IAM = """
roles:
- name: eks-cluster
  description: EKS control plane
  assumed_by: eks.amazonaws.com
  managed_policies:
  - AmazonEKSClusterPolicy
- name: eks-nodes
  assumed_by: ec2.amazonaws.com
  managed_policies:
  - AmazonEKSWorkerNodePolicy
  - AmazonEC2ContainerRegistryPullOnly
  - AmazonEKS_CNI_Policy
instance_profiles:
- name: eks-nodes
  role: eks-nodes
"""


def _stack(write_env, config=IAM, *, env_name="test"):
    environment = load_environment(
        env_name, root=write_env(NETWORK, iam=config, name=env_name)
    )
    app = cdk.App()
    stack = IamStack(
        app,
        f"{env_name}-iam",
        environment=environment,
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    return app, stack


def test_role_trust_is_service_specific(write_env):
    _, stack = _stack(write_env)
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::IAM::Role", 2)
    for name, service in (
        ("eks-cluster", "eks.amazonaws.com"), ("eks-nodes", "ec2.amazonaws.com")
    ):
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "RoleName": f"int-production-use1-test-{name}-role",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Principal": {"Service": service},
                        }
                    ],
                },
            },
        )


def test_only_requested_managed_policies_are_attached(write_env):
    _, stack = _stack(write_env)
    template = Template.from_stack(stack)
    roles = template.find_resources("AWS::IAM::Role")
    expected = {
        "eks-cluster": ["AmazonEKSClusterPolicy"],
        "eks-nodes": [
            "AmazonEKSWorkerNodePolicy",
            "AmazonEC2ContainerRegistryPullOnly",
            "AmazonEKS_CNI_Policy",
        ],
    }
    for name, policies in expected.items():
        role = next(
            resource["Properties"]
            for resource in roles.values()
            if resource["Properties"]["RoleName"].endswith(f"-{name}-role")
        )
        assert role["ManagedPolicyArns"] == [
            {"Fn::Join": [
                "", ["arn:", {"Ref": "AWS::Partition"}, f":iam::aws:policy/{policy}"]
            ]}
            for policy in policies
        ]
        assert "Policies" not in role


def test_profile_references_only_the_node_role(write_env):
    _, stack = _stack(write_env)
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::IAM::InstanceProfile", 1)
    roles = template.find_resources("AWS::IAM::Role")
    node_id = next(
        logical_id
        for logical_id, resource in roles.items()
        if resource["Properties"]["RoleName"].endswith("-eks-nodes-role")
    )
    template.has_resource_properties(
        "AWS::IAM::InstanceProfile",
        {
            "InstanceProfileName": "int-production-use1-test-eks-nodes-profile",
            "Roles": [{"Ref": node_id}],
        },
    )


def test_common_and_name_tags_are_applied_to_roles(write_env):
    _, stack = _stack(write_env)
    roles = Template.from_stack(stack).find_resources("AWS::IAM::Role")
    for resource in roles.values():
        props = resource["Properties"]
        tags = {tag["Key"]: tag["Value"] for tag in props["Tags"]}
        assert tags["Name"] == props["RoleName"]
        assert tags["Compliance"] == "hippaa"
        assert tags["Environment"] == "test"


def test_role_and_profile_names_and_arns_are_exported(write_env):
    _, stack = _stack(write_env)
    outputs = Template.from_stack(stack).find_outputs("*")
    assert {output["Export"]["Name"] for output in outputs.values()} == {
        f"test-iam-{name}-{kind}-{attribute}"
        for name, kind in (
            ("eks-cluster", "role"), ("eks-nodes", "role"), ("eks-nodes", "profile")
        )
        for attribute in ("name", "arn")
    }
    assert stack.iam.roles.keys() == {"eks-cluster", "eks-nodes"}
    assert stack.iam.instance_profiles.keys() == {"eks-nodes"}


def test_iam_needs_no_network_or_security_stack(write_env):
    app, stack = _stack(write_env)
    assembly = app.synth()
    assert stack.dependencies == []
    template = assembly.get_stack_by_name(stack.stack_name).template
    assert "Fn::ImportValue" not in str(template)
    assert not any(
        resource["Type"].startswith("AWS::EC2::")
        for resource in template["Resources"].values()
    )


def test_names_are_environment_scoped(write_env):
    for env_name in ("dev", "prod"):
        _, stack = _stack(write_env, env_name=env_name)
        template = Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::IAM::Role",
            {"RoleName": f"int-production-use1-{env_name}-eks-nodes-role"},
        )
        template.has_resource_properties(
            "AWS::IAM::InstanceProfile",
            {"InstanceProfileName": f"int-production-use1-{env_name}-eks-nodes-profile"},
        )


def test_roles_can_be_created_without_policies_or_profiles(write_env):
    _, stack = _stack(write_env, "roles:\n- name: bare\n  assumed_by: ec2.amazonaws.com")
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::IAM::InstanceProfile", 0)
    roles = template.find_resources("AWS::IAM::Role")
    props = next(iter(roles.values()))["Properties"]
    assert "ManagedPolicyArns" not in props


def test_environment_without_iam_remains_valid(write_env):
    environment = load_environment("test", root=write_env(NETWORK))
    assert environment.iam == IamProps()


def test_aws_managed_policy_paths_are_preserved():
    props = IamProps.from_dict(
        {
            "roles": [
                {
                    "name": "service",
                    "assumed_by": "ec2.amazonaws.com",
                    "managed_policies": ["service-role/AmazonEC2RoleforSSM"],
                }
            ],
        }
    )
    assert props.roles[0].managed_policies == ("service-role/AmazonEC2RoleforSSM",)


@pytest.mark.parametrize(
    "config,message",
    [
        ([], "iam must be a mapping"),
        ({"unknown": []}, "unknown fields"),
        ({"roles": {}}, "must be lists"),
        ({"instance_profiles": None}, "must be lists"),
        ({"roles": ["bad"]}, "IAM role must be a mapping"),
        ({"roles": [{}]}, "IAM role name"),
        ({"roles": [{"name": "bad/name"}]}, "IAM role name"),
        ({"roles": [{"name": "bad", "assumed_by": "*"}]}, "assumed_by"),
        ({"instance_profiles": [{"name": "nodes", "role": "missing"}]}, "unknown role"),
        ({"instance_profiles": [{"name": "nodes"}]}, "Instance profile role"),
        (
            {"roles": [
                {"name": "nodes", "assumed_by": "ec2.amazonaws.com"},
                {"name": "NODES", "assumed_by": "ec2.amazonaws.com"},
            ]},
            "duplicate role",
        ),
        (
            {
                "roles": [{"name": "cluster", "assumed_by": "eks.amazonaws.com"}],
                "instance_profiles": [{"name": "cluster", "role": "cluster"}],
            },
            "must trust EC2",
        ),
        (
            {"instance_profiles": [
                {"name": "nodes", "role": "nodes"},
                {"name": "NODES", "role": "nodes"},
            ]},
            "duplicate instance profile",
        ),
    ],
)
def test_invalid_iam_config_fails_explicitly(config, message):
    with pytest.raises(ValueError, match=message):
        IamProps.from_dict(config)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"description": 1}, "description"),
        ({"managed_policies": "ReadOnlyAccess"}, "managed_policies"),
        (
            {"managed_policies": ["arn:aws:iam::aws:policy/ReadOnlyAccess"]},
            "not ARNs",
        ),
        ({"managed_policies": ["/"]}, "managed_policies"),
        ({"managed_policies": ["ReadOnlyAccess", "ReadOnlyAccess"]}, "duplicate"),
    ],
)
def test_invalid_role_fields_fail_explicitly(overrides, message):
    role = {"name": "nodes", "assumed_by": "ec2.amazonaws.com", **overrides}
    with pytest.raises(ValueError, match=message):
        IamProps.from_dict({"roles": [role]})


def test_final_role_name_limit_is_checked(write_env):
    environment = load_environment("test", root=write_env(NETWORK, iam=IAM))
    environment = replace(
        environment, common=replace(environment.common, account_name="a" * 60)
    )
    with pytest.raises(ValueError, match="at most 64"):
        IamStack(cdk.App(), "too-long", environment=environment)


def test_dev_has_expected_roles_and_profile():
    environment = load_environment("dev")
    assert {role.name for role in environment.iam.roles} == {
        "eks-cluster", "eks-nodes", "ec2", "default", "source-replication",
    }
    assert [(profile.name, profile.role) for profile in environment.iam.instance_profiles] == [
        ("eks-nodes", "eks-nodes"), ("ec2", "ec2"),
    ]
    assert load_environment("prod").iam == IamProps()


def test_dev_node_role_includes_custom_test_policy():
    environment = load_environment("dev")
    stack = IamStack(
        cdk.App(), "dev-iam", environment=environment,
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    template = Template.from_stack(stack)
    template.has_resource_properties("AWS::IAM::Role", {
        "RoleName": environment.common.name("dev", "eks-nodes", "role"),
        "Policies": [{
            "PolicyName": "TestReadConfig",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "ReadExampleConfig",
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": "arn:aws:s3:::example-config-bucket/config/*",
                    "Condition": {"Bool": {"aws:SecureTransport": "true"}},
                }],
            },
        }],
    })


TRUST = {
    "Version": "2012-10-17",
    "Id": "CustomTrust",
    "Statement": [
        {
            "Sid": "Ec2Trust",
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        },
        {
            "Sid": "ExternalTrust",
            "Effect": "Allow",
            "Principal": {"AWS": "arn:aws:iam::111122223333:role/trusted-automation"},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Condition": {"StringEquals": {"sts:ExternalId": "example-external-id"}},
        },
        {
            "Sid": "DenyWithoutMfa",
            "Effect": "Deny",
            "Principal": {"AWS": "arn:aws:iam::111122223333:role/trusted-automation"},
            "Action": "sts:AssumeRole",
            "Condition": {"Bool": {"aws:MultiFactorAuthPresent": "false"}},
        },
    ],
}
PERMISSIONS = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ReadObjects",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": "arn:aws:s3:::example-bucket/config/*",
            "Condition": {"Bool": {"aws:SecureTransport": "true"}},
        }
    ],
}
POLICY_ARN = "arn:aws:iam::111122223333:policy/team/CustomWorkerPolicy"


def _custom_config(*, explicit_trust=True):
    return {
        "roles": [{
            "name": "eks-nodes",
            **({"trust_policy": TRUST} if explicit_trust else {"assumed_by": "ec2.amazonaws.com"}),
            "managed_policies": ["AmazonEKSWorkerNodePolicy"],
            "managed_policy_arns": [POLICY_ARN],
            "custom_policies": {"ReadConfig": PERMISSIONS},
        }],
        "instance_profiles": [{"name": "eks-nodes", "role": "eks-nodes"}],
    }


@pytest.mark.parametrize("explicit_trust", [False, True])
def test_custom_permissions_work_with_both_trust_formats(write_env, explicit_trust):
    config = _custom_config(explicit_trust=explicit_trust)
    _, stack = _stack(write_env, yaml.safe_dump(config))
    template = Template.from_stack(stack)
    role = next(iter(template.find_resources("AWS::IAM::Role").values()))["Properties"]
    assert role["ManagedPolicyArns"] == [
        {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::aws:policy/AmazonEKSWorkerNodePolicy"]]},
        POLICY_ARN,
    ]
    assert role["Policies"] == [{"PolicyName": "ReadConfig", "PolicyDocument": PERMISSIONS}]
    if explicit_trust:
        assert role["AssumeRolePolicyDocument"] == TRUST
    template.resource_count_is("AWS::IAM::InstanceProfile", 1)
    template.resource_count_is("AWS::IAM::ManagedPolicy", 0)


def test_switching_to_explicit_trust_preserves_logical_ids_and_exports(write_env):
    templates = []
    for explicit_trust in (False, True):
        config = _custom_config(explicit_trust=explicit_trust)
        _, stack = _stack(write_env, yaml.safe_dump(config))
        templates.append(Template.from_stack(stack))
    for resource_type in ("AWS::IAM::Role", "AWS::IAM::InstanceProfile"):
        assert templates[0].find_resources(resource_type).keys() == templates[1].find_resources(resource_type).keys()
    assert templates[0].find_outputs("*").keys() == templates[1].find_outputs("*").keys()
    for template in templates:
        role = next(iter(template.find_resources("AWS::IAM::Role").values()))["Properties"]
        assert role["RoleName"] == "int-production-use1-test-eks-nodes-role"
        tags = {tag["Key"]: tag["Value"] for tag in role["Tags"]}
        assert tags["Compliance"] == "hippaa"
        assert tags["Name"] == role["RoleName"]


def test_profile_and_exports_reference_custom_trust_role(write_env):
    _, stack = _stack(write_env, yaml.safe_dump(_custom_config()))
    template = Template.from_stack(stack)
    role_id = next(iter(template.find_resources("AWS::IAM::Role")))
    profile = next(iter(template.find_resources("AWS::IAM::InstanceProfile").values()))
    # The imported L2 facade still uses the created role's ARN token, not a
    # literal name, keeping the role/profile dependency in CloudFormation.
    assert json.dumps({"Fn::GetAtt": [role_id, "Arn"]}) in json.dumps(profile["Properties"]["Roles"])
    outputs = template.find_outputs("*")
    assert any(output["Value"] == {"Fn::GetAtt": [role_id, "Arn"]} for output in outputs.values())


def test_custom_trust_role_can_receive_further_cdk_permissions(write_env):
    _, stack = _stack(write_env, yaml.safe_dump(_custom_config()))
    result = stack.iam.roles["eks-nodes"].add_to_principal_policy(
        iam.PolicyStatement(actions=["ec2:DescribeInstances"], resources=["*"])
    )
    assert result.statement_added
    Template.from_stack(stack).resource_count_is("AWS::IAM::Policy", 1)


@pytest.mark.parametrize("explicit_trust", [False, True])
def test_custom_policy_version_id_and_not_fields_are_preserved(write_env, explicit_trust):
    document = {
        "Version": "2008-10-17",
        "Id": "LegacyPolicy",
        "Statement": [{
            "Effect": "Deny",
            "NotAction": ["s3:GetObject", "s3:ListBucket"],
            "NotResource": "arn:aws:s3:::example-bucket/*",
        }],
    }
    config = _custom_config(explicit_trust=explicit_trust)
    config["roles"][0]["custom_policies"] = {"Legacy": document}
    _, stack = _stack(write_env, yaml.safe_dump(config))
    role = next(iter(Template.from_stack(stack).find_resources("AWS::IAM::Role").values()))
    assert role["Properties"]["Policies"] == [{
        "PolicyName": "Legacy", "PolicyDocument": document,
    }]


def test_federated_trust_and_single_statement_are_preserved(write_env):
    trust = {
        "Version": "2012-10-17",
        "Statement": {
            "Effect": "Allow",
            "Principal": {"Federated": "arn:aws:iam::111122223333:oidc-provider/example.com"},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {"example.com:sub": "system:serviceaccount:default:app"}},
        },
    }
    _, stack = _stack(write_env, yaml.safe_dump({"roles": [{"name": "app", "trust_policy": trust}]}))
    template = Template.from_stack(stack)
    role = next(iter(template.find_resources("AWS::IAM::Role").values()))["Properties"]
    assert role["AssumeRolePolicyDocument"] == trust
    assert "ManagedPolicyArns" not in role
    assert "Policies" not in role


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({}, "exactly one"),
        ({"assumed_by": "ec2.amazonaws.com", "trust_policy": TRUST}, "exactly one"),
        ({"assumed_by": None}, "assumed_by"),
        ({"trust_policy": None}, "must be a mapping"),
        ({"trust_policy": {}}, "Statement"),
        ({"trust_policy": {"Statement": []}}, "Statement"),
        ({"trust_policy": {"Statement": [1]}}, "must be a mapping"),
        ({"trust_policy": {"Version": 20121017, "Statement": TRUST["Statement"]}}, "Version"),
        ({"assumed_by": "ec2.amazonaws.com", "managed_policy_arns": "arn:bad"}, "managed_policy_arns"),
        ({"assumed_by": "ec2.amazonaws.com", "managed_policy_arns": ["arn:aws:s3:::bucket"]}, "managed_policy_arns"),
        ({"assumed_by": "ec2.amazonaws.com", "managed_policy_arns": [POLICY_ARN, POLICY_ARN]}, "duplicate"),
        ({"assumed_by": "ec2.amazonaws.com", "managed_policy_arns": ["arn:aws:iam::aws:policy/ReadOnlyAccess"], "managed_policies": ["ReadOnlyAccess"]}, "duplicate"),
        ({"assumed_by": "ec2.amazonaws.com", "custom_policies": []}, "custom_policies"),
        ({"assumed_by": "ec2.amazonaws.com", "custom_policies": {"bad/name": PERMISSIONS}}, "policy name"),
        ({"assumed_by": "ec2.amazonaws.com", "custom_policies": {"Empty": {}}}, "Statement"),
    ],
)
def test_invalid_custom_role_fields(overrides, message):
    with pytest.raises(ValueError, match=message):
        IamProps.from_dict({"roles": [{"name": "custom", **overrides}]})


@pytest.mark.parametrize(
    "trust,statement,message",
    [
        (True, {"Effect": "Allow", "Action": "sts:AssumeRole"}, "Principal"),
        (True, {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {}}, "empty"),
        (True, {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": []}}, "nonempty"),
        (True, {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": "*", "Resource": "*"}, "Resource"),
        (True, {"Effect": "Maybe", "Action": "sts:AssumeRole", "Principal": "*"}, "Effect"),
        (True, {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": "*", "Condition": []}, "Condition"),
        (True, {"Effect": "Allow", "Action": [], "Principal": "*"}, "nonempty"),
        (True, {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": "*", "Typo": 1}, "unknown fields"),
        (False, {"Effect": "Allow", "Action": "s3:GetObject"}, "Resource"),
        (False, {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*", "Principal": "*"}, "Principal"),
        (False, {"Effect": "Allow", "Action": "s3:GetObject", "NotAction": "s3:DeleteObject", "Resource": "*"}, "exactly one"),
        (False, {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*", "NotResource": "*"}, "exactly one"),
    ],
)
def test_invalid_policy_statements(trust, statement, message):
    document = {"Version": "2012-10-17", "Statement": [statement]}
    role = {"name": "custom"}
    if trust:
        role["trust_policy"] = document
    else:
        role.update(assumed_by="ec2.amazonaws.com", custom_policies={"Bad": document})
    with pytest.raises(ValueError, match=message):
        IamProps.from_dict({"roles": [role]})


@pytest.mark.parametrize("trust", [
    {"Statement": [{"Effect": "Deny", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
    {"Statement": [{"Effect": "Allow", "Principal": {"Service": "eks.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
    {"Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:TagSession"}]},
    {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]},
])
def test_instance_profiles_need_explicit_ec2_allow(trust):
    with pytest.raises(ValueError, match="must trust EC2"):
        IamProps.from_dict({
            "roles": [{"name": "nodes", "trust_policy": trust}],
            "instance_profiles": [{"name": "nodes", "role": "nodes"}],
        })


def test_instance_profile_accepts_custom_ec2_trust_with_conditions():
    trust = {
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": ["ssm.amazonaws.com", "ec2.amazonaws.com"]},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Condition": {"StringEquals": {"aws:SourceAccount": "111122223333"}},
        }],
    }
    props = IamProps.from_dict({
        "roles": [{"name": "nodes", "trust_policy": trust}],
        "instance_profiles": [{"name": "nodes", "role": "nodes"}],
    })
    assert props.roles[0].trusts_ec2


def test_dev_trust_format_change_preserves_existing_role_resources():
    environment = load_environment("dev")
    legacy = replace(
        environment.iam,
        roles=tuple(
            replace(role, assumed_by="eks.amazonaws.com", trust_policy=None)
            if role.name == "eks-cluster" else role
            for role in environment.iam.roles
        ),
    )
    templates = []
    for props in (legacy, environment.iam):
        stack = IamStack(
            cdk.App(), "iam",
            environment=replace(environment, iam=props),
            env=cdk.Environment(account=environment.account_id, region=environment.region),
        )
        templates.append(Template.from_stack(stack))
    for resource_type in ("AWS::IAM::Role", "AWS::IAM::InstanceProfile"):
        assert templates[0].find_resources(resource_type) == templates[1].find_resources(resource_type)


@pytest.mark.parametrize("file_trust", [False, True])
@pytest.mark.parametrize("file_permissions", [False, True])
def test_policy_files_match_embedded_documents(
    write_env, tmp_path, monkeypatch, file_trust, file_permissions
):
    config = _custom_config()
    role = config["roles"][0]
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir(parents=True)
    trust_file = policy_dir / "trust.yaml"
    permissions_file = policy_dir / "permissions.yml"
    trust_file.write_text(yaml.safe_dump(TRUST), encoding="utf-8")
    permissions_file.write_text(yaml.safe_dump(PERMISSIONS), encoding="utf-8")

    _, embedded_stack = _stack(write_env, yaml.safe_dump(config))
    expected = Template.from_stack(embedded_stack).to_json()
    if file_trust:
        role["trust_policy"] = f"{tmp_path.name}/policies/trust.yaml"
    if file_permissions:
        role["custom_policies"]["ReadConfig"] = (
            f"{tmp_path.name}/policies/permissions.yml"
        )
    root = write_env(NETWORK, iam=yaml.safe_dump(config))
    # Files are relative to env.yaml, not the app's working directory.
    monkeypatch.chdir(tmp_path.parent.parent)
    environment = load_environment("test", root=root)
    app = cdk.App()
    stack = IamStack(
        app, "test-iam", environment=environment,
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    assert Template.from_stack(stack).to_json() == expected


def test_embedded_and_file_permissions_can_be_mixed(write_env, tmp_path):
    config = _custom_config()
    config["roles"][0]["custom_policies"]["FromFile"] = (
        f"{tmp_path.name}/policies/extra.yaml"
    )
    root = write_env(NETWORK, iam=yaml.safe_dump(config))
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "extra.yaml").write_text(
        yaml.safe_dump(PERMISSIONS), encoding="utf-8"
    )
    environment = load_environment("test", root=root)
    assert environment.iam.roles[0].custom_policies == {
        "ReadConfig": PERMISSIONS, "FromFile": PERMISSIONS,
    }


@pytest.mark.parametrize("field", ["trust_policy", "custom_policies"])
@pytest.mark.parametrize(
    "contents,message",
    [
        (None, "cannot read policy file"),
        ("Statement: [", "invalid YAML"),
        ("", "must be a mapping"),
        ("- not\n- a policy\n", "must be a mapping"),
        ("some-other-policy.yaml", "must be a mapping"),
        ("Version: '2012-10-17'\nStatement: []", "Statement"),
        (
            "Version: 2012-10-17\nStatement:\n"
            "- Effect: Allow\n  Action: sts:AssumeRole\n"
            "  Principal: {Service: ec2.amazonaws.com}\n  Resource: '*'\n",
            "Version",
        ),
    ],
)
def test_bad_policy_files_report_role_and_file(write_env, tmp_path, field, contents, message):
    role = {"name": "from-file"}
    if field == "trust_policy":
        role["trust_policy"] = f"{tmp_path.name}/policies/bad.yaml"
    else:
        role.update(
            assumed_by="ec2.amazonaws.com",
            custom_policies={"Custom": f"{tmp_path.name}/policies/bad.yaml"},
        )
    root = write_env(NETWORK, iam=yaml.safe_dump({"roles": [role]}))
    if contents is not None:
        policy_dir = tmp_path / "policies"
        policy_dir.mkdir()
        (policy_dir / "bad.yaml").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match=message) as error:
        load_environment("test", root=root)
    assert "from-file" in str(error.value)
    assert "bad.yaml" in str(error.value)
    if field == "custom_policies":
        assert "Custom" in str(error.value)


@pytest.mark.parametrize(
    "path",
    [
        "", " ", "/absolute.yaml", r"C:\policies\trust.yaml",
        "https://example.com/trust.yaml", "../outside.yaml",
    ],
)
def test_policy_file_paths_must_be_local_and_relative(tmp_path, path):
    with pytest.raises(ValueError, match="relative"):
        IamProps.from_dict(
            {"roles": [{"name": "bad", "trust_policy": path}]}, base_dir=tmp_path
        )


def test_policy_file_requires_explicit_base_directory():
    with pytest.raises(ValueError, match="base_dir"):
        IamProps.from_dict({"roles": [{"name": "bad", "trust_policy": "trust.yaml"}]})


def test_file_trust_does_not_bypass_instance_profile_validation(write_env, tmp_path):
    config = {
        "roles": [{
            "name": "not-ec2",
            "trust_policy": f"{tmp_path.name}/policies/trust.yaml",
        }],
        "instance_profiles": [{"name": "nodes", "role": "not-ec2"}],
    }
    root = write_env(NETWORK, iam=yaml.safe_dump(config))
    trust = {
        "Statement": [{
            "Effect": "Allow", "Action": "sts:AssumeRole",
            "Principal": {"Service": "eks.amazonaws.com"},
        }],
    }
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "trust.yaml").write_text(yaml.safe_dump(trust), encoding="utf-8")
    with pytest.raises(ValueError, match="must trust EC2"):
        load_environment("test", root=root)


def test_file_trust_and_service_shorthand_cannot_be_combined(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        IamProps.from_dict({
            "roles": [{
                "name": "ambiguous", "assumed_by": "ec2.amazonaws.com",
                "trust_policy": "missing.yaml",
            }],
        }, base_dir=tmp_path)


# --- Customer-managed policies, extended role fields and users ---------------

READ = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "ReadBucket",
        "Effect": "Allow",
        "Action": "s3:GetObject",
        "Resource": "arn:[[partition]]:s3:::[[account_name]]-[[account_number]]-[[region]]/*",
    }],
}
RENDERED_RESOURCE = {"Fn::Join": ["", [
    "arn:", {"Ref": "AWS::Partition"}, ":s3:::int-production-111122223333-us-east-1/*",
]]}


def _props_stack(config, *, base_dir=None, env_name="test", write_env=None):
    environment = load_environment(env_name, root=write_env(NETWORK, name=env_name))
    stack = IamStack(
        cdk.App(), f"{env_name}-iam", environment=environment,
        props=IamProps.from_dict(config, base_dir=base_dir),
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    return stack, Template.from_stack(stack)


def _users_config(**user):
    return {
        "policies": [{"name": "shared-read", "document": READ}],
        "users": [{
            "name": "deployer",
            "path": "/automation/",
            "groups": ["ops"],
            "managed_policies": ["ReadOnlyAccess"],
            "customer_managed_policies": ["shared-read"],
            "policy": {"name": "deployer-own", "description": "Deployer", "document": READ},
            "group_policies": [{
                "group_name": "ops", "policy_name": "ops-read", "document": READ,
            }],
            **user,
        }],
    }


def test_customer_managed_policy_renders_placeholders(write_env):
    config = {"policies": [{
        "name": "app-read", "description": "App read", "path": "/app/", "document": READ,
    }]}
    stack, template = _props_stack(config, write_env=write_env)
    template.resource_count_is("AWS::IAM::ManagedPolicy", 1)
    template.has_resource_properties("AWS::IAM::ManagedPolicy", {
        "ManagedPolicyName": "int-production-use1-test-app-read-policy",
        "Description": "App read",
        "Path": "/app/",
        "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "ReadBucket", "Effect": "Allow", "Action": "s3:GetObject",
                "Resource": RENDERED_RESOURCE,
            }],
        },
    })
    assert stack.iam.managed_policies.keys() == {"app-read"}
    policy_id = next(iter(template.find_resources("AWS::IAM::ManagedPolicy")))
    outputs = {
        output["Export"]["Name"]: output["Value"]
        for output in template.find_outputs("*").values()
    }
    assert outputs == {
        "test-iam-app-read-policy-arn": {"Ref": policy_id},
        "test-iam-app-read-policy-name": "int-production-use1-test-app-read-policy",
    }


def test_policy_document_file_matches_embedded(write_env, tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "read.json").write_text(json.dumps(READ), encoding="utf-8")
    embedded = {"policies": [{"name": "app-read", "document": READ}]}
    from_file = {"policies": [{"name": "app-read", "document": "policies/read.json"}]}
    _, expected = _props_stack(embedded, write_env=write_env)
    _, actual = _props_stack(from_file, base_dir=tmp_path, write_env=write_env)
    assert actual.to_json() == expected.to_json()


def test_iam_stack_with_only_policies_or_users_is_truthy():
    assert not IamProps()
    assert IamProps.from_dict({"policies": [{"name": "p", "document": READ}]})
    assert IamProps.from_dict({"users": [{"name": "u"}]})
    assert IamProps.from_dict({"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com"}]})


@pytest.mark.parametrize("explicit_trust", [False, True])
def test_roles_attach_customer_managed_policies_and_new_fields(write_env, explicit_trust):
    role = {
        "name": "app",
        **({"trust_policy": TRUST} if explicit_trust else {"assumed_by": "ec2.amazonaws.com"}),
        "path": "/app/",
        "max_session_duration": 7200,
        "permissions_boundary": "boundary",
        "managed_policies": ["AmazonSSMManagedInstanceCore"],
        "customer_managed_policies": ["app-read"],
    }
    config = {
        "policies": [
            {"name": "app-read", "document": READ},
            {"name": "boundary", "document": READ},
        ],
        "roles": [role],
        "instance_profiles": [{"name": "app", "role": "app"}],
    }
    stack, template = _props_stack(config, write_env=write_env)
    policies = template.find_resources("AWS::IAM::ManagedPolicy")
    ids = {
        resource["Properties"]["ManagedPolicyName"].split("-test-")[1]: logical_id
        for logical_id, resource in policies.items()
    }
    role_id, resource = next(iter(template.find_resources("AWS::IAM::Role").items()))
    props = resource["Properties"]
    assert props["Path"] == "/app/"
    assert props["MaxSessionDuration"] == 7200
    assert props["PermissionsBoundary"] == {"Ref": ids["boundary-policy"]}
    assert props["ManagedPolicyArns"] == [
        {"Fn::Join": ["", [
            "arn:", {"Ref": "AWS::Partition"}, ":iam::aws:policy/AmazonSSMManagedInstanceCore",
        ]]},
        {"Ref": ids["app-read-policy"]},
    ]
    if explicit_trust:
        assert props["AssumeRolePolicyDocument"] == TRUST
    # A pathed role's name is its Ref, not a segment parsed from its ARN.
    template.has_resource_properties(
        "AWS::IAM::InstanceProfile", {"Roles": [{"Ref": role_id}]}
    )
    outputs = template.find_outputs("*")
    assert outputs["appRoleName"]["Value"] == {"Ref": role_id}
    assert stack.iam.managed_policies.keys() == {"app-read", "boundary"}


def test_permissions_boundary_accepts_policy_arn(write_env):
    arn = "arn:aws:iam::111122223333:policy/Boundary"
    _, template = _props_stack(
        {"roles": [{
            "name": "app", "assumed_by": "ec2.amazonaws.com", "permissions_boundary": arn,
        }]},
        write_env=write_env,
    )
    template.has_resource_properties("AWS::IAM::Role", {"PermissionsBoundary": arn})


def test_custom_trust_role_without_path_keeps_original_template_shape(write_env):
    config = _custom_config()
    config["roles"][0]["max_session_duration"] = 3600
    _, template = _props_stack(config, write_env=write_env)
    role_id = next(iter(template.find_resources("AWS::IAM::Role")))
    profile = next(iter(template.find_resources("AWS::IAM::InstanceProfile").values()))
    assert json.dumps({"Fn::GetAtt": [role_id, "Arn"]}) in json.dumps(
        profile["Properties"]["Roles"]
    )
    template.has_resource_properties("AWS::IAM::Role", {"MaxSessionDuration": 3600})


def test_adding_policies_and_users_does_not_change_existing_roles():
    environment = load_environment("dev")
    extended = replace(
        environment.iam,
        policies=environment.iam.policies + IamProps.from_dict(_users_config()).policies,
        users=IamProps.from_dict(_users_config()).users,
    )
    templates = []
    for props in (environment.iam, extended):
        stack = IamStack(
            cdk.App(), "dev-iam", environment=environment, props=props,
            env=cdk.Environment(account=environment.account_id, region=environment.region),
        )
        templates.append(Template.from_stack(stack))
    for resource_type in ("AWS::IAM::Role", "AWS::IAM::InstanceProfile"):
        assert templates[0].find_resources(resource_type) == templates[1].find_resources(resource_type)
    before = templates[0].find_outputs("*")
    after = templates[1].find_outputs("*")
    assert {key: after[key] for key in before} == before


def test_role_trust_and_custom_policies_render_placeholders(write_env):
    trust = {"Statement": [{
        "Effect": "Allow", "Action": "sts:AssumeRole",
        "Principal": {"AWS": "arn:[[partition]]:iam::[[account_number]]:root"},
    }]}
    _, template = _props_stack(
        {"roles": [{"name": "app", "trust_policy": trust, "custom_policies": {"Read": READ}}]},
        write_env=write_env,
    )
    role = next(iter(template.find_resources("AWS::IAM::Role").values()))["Properties"]
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
        "AWS": {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::111122223333:root"]]}
    }
    assert role["Policies"][0]["PolicyDocument"]["Statement"][0]["Resource"] == RENDERED_RESOURCE
    template.resource_count_is("Custom::DeployTimeLookup", 0)


def test_sso_role_placeholders_resolve_at_deploy_time(write_env):
    trust = {"Statement": [{
        "Effect": "Allow", "Action": "sts:AssumeRole",
        "Principal": {"AWS": ["[[admin_role]]", "[[network_role]]"]},
    }]}
    config = {
        "roles": [
            {"name": "a", "trust_policy": trust},
            {"name": "b", "trust_policy": trust},
        ],
    }
    _, template = _props_stack(config, write_env=write_env)
    lookups = template.find_resources("Custom::DeployTimeLookup")
    assert sorted(resource["Properties"]["PermissionSet"] for resource in lookups.values()) == [
        "AdministratorAccess", "NetworkAdministrator",
    ]
    ids = {resource["Properties"]["PermissionSet"]: key for key, resource in lookups.items()}
    roles = template.find_resources("AWS::IAM::Role")
    role = next(
        resource["Properties"] for resource in roles.values()
        if resource["Properties"].get("RoleName", "").endswith("-a-role")
    )
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["AWS"] == [
        {"Fn::GetAtt": [ids["AdministratorAccess"], "Value"]},
        {"Fn::GetAtt": [ids["NetworkAdministrator"], "Value"]},
    ]


def test_user_groups_and_policies_without_access_key(write_env):
    stack, template = _props_stack(_users_config(), write_env=write_env)
    template.resource_count_is("AWS::IAM::User", 1)
    template.resource_count_is("AWS::IAM::Group", 1)
    template.resource_count_is("AWS::IAM::ManagedPolicy", 3)
    template.resource_count_is("AWS::IAM::AccessKey", 0)
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    policy_ids = {
        resource["Properties"]["ManagedPolicyName"]: logical_id
        for logical_id, resource in template.find_resources("AWS::IAM::ManagedPolicy").items()
    }
    group_id = next(iter(template.find_resources("AWS::IAM::Group")))
    template.has_resource_properties("AWS::IAM::Group", {
        "GroupName": "int-production-use1-test-ops-group",
        "ManagedPolicyArns": [{"Ref": policy_ids["int-production-use1-test-ops-read-policy"]}],
    })
    user = next(iter(template.find_resources("AWS::IAM::User").values()))["Properties"]
    assert user["UserName"] == "int-production-use1-test-deployer-user"
    assert user["Path"] == "/automation/"
    assert user["Groups"] == [{"Ref": group_id}]
    assert user["ManagedPolicyArns"] == [
        {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::aws:policy/ReadOnlyAccess"]]},
        {"Ref": policy_ids["int-production-use1-test-shared-read-policy"]},
        {"Ref": policy_ids["int-production-use1-test-deployer-own-policy"]},
    ]
    tags = {tag["Key"]: tag["Value"] for tag in user["Tags"]}
    assert tags["Name"] == user["UserName"]
    assert tags["Compliance"] == "hippaa"
    assert stack.iam.users.keys() == {"deployer"}
    assert stack.iam.groups.keys() == {"ops"}
    assert stack.iam.managed_policies.keys() == {"shared-read", "deployer-own", "ops-read"}
    assert stack.iam.user_secrets == {}
    assert {output["Export"]["Name"] for output in template.find_outputs("*").values()} == {
        f"test-iam-{name}-{kind}-{attribute}"
        for name, kind in (
            ("shared-read", "policy"), ("deployer-own", "policy"), ("ops-read", "policy"),
            ("ops", "group"), ("deployer", "user"),
        )
        for attribute in ("arn", "name")
    }


def test_shared_groups_are_created_once(write_env):
    config = {"users": [
        {"name": "a", "groups": ["ops"]},
        {"name": "b", "groups": ["ops", "audit"]},
    ]}
    _, template = _props_stack(config, write_env=write_env)
    template.resource_count_is("AWS::IAM::Group", 2)
    groups = template.find_resources("AWS::IAM::Group")
    assert "ManagedPolicyArns" not in next(iter(groups.values()))["Properties"]


def test_access_key_secret_never_exposes_secret_material(write_env):
    stack, template = _props_stack(
        _users_config(create_access_key=True), write_env=write_env
    )
    template.resource_count_is("AWS::IAM::AccessKey", 1)
    key_id = next(iter(template.find_resources("AWS::IAM::AccessKey")))
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == 1
    secret_id, secret = next(iter(secrets.items()))
    assert secret["DeletionPolicy"] == "Retain"
    assert secret["UpdateReplacePolicy"] == "Retain"
    props = secret["Properties"]
    assert props["Name"] == "int-production-use1-test-deployer-credentials"
    joined = props["SecretString"]["Fn::Join"][1]
    assert {"Fn::GetAtt": [key_id, "SecretAccessKey"]} in joined
    assert {"Ref": key_id} in joined
    assert "GenerateSecretString" not in props
    rendered = json.dumps(template.to_json())
    # The secret access key is only ever a CloudFormation reference inside the secret.
    assert rendered.count('"SecretAccessKey"') == 1
    assert "SecretAccessKey" not in json.dumps(template.find_outputs("*"))
    assert "AccessKeyId" not in json.dumps(template.find_outputs("*"))
    outputs = template.find_outputs("*")
    assert outputs["deployerCredentialsSecretArn"] == {
        "Value": {"Ref": secret_id},
        "Export": {"Name": "test-iam-deployer-user-credentials-secret-arn"},
    }
    assert stack.iam.user_secrets.keys() == {"deployer"}


def test_access_key_secret_options(write_env):
    secret_policy = {"Statement": [{
        "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "*",
        "Principal": {"AWS": "arn:aws:iam::[[account_number]]:role/reader"},
    }]}
    _, template = _props_stack(
        _users_config(create_access_key=True, secrets_manager={
            "name": "deploy-keys", "description": "Deploy keys",
            "removal_policy": "destroy", "policy": secret_policy,
        }),
        write_env=write_env,
    )
    secret_id, secret = next(iter(template.find_resources("AWS::SecretsManager::Secret").items()))
    assert secret["DeletionPolicy"] == "Delete"
    assert secret["Properties"]["Name"] == "int-production-use1-test-deploy-keys-credentials"
    assert secret["Properties"]["Description"] == "Deploy keys"
    template.has_resource_properties("AWS::SecretsManager::ResourcePolicy", {
        "SecretId": {"Ref": secret_id},
        "BlockPublicPolicy": True,
        "ResourcePolicy": {"Statement": [{
            "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "*",
            "Principal": {"AWS": "arn:aws:iam::111122223333:role/reader"},
        }]},
    })


def test_final_user_and_policy_name_limits_are_checked(write_env):
    environment = load_environment("test", root=write_env(NETWORK))
    environment = replace(
        environment, common=replace(environment.common, account_name="a" * 60)
    )
    for config, message in (
        ({"users": [{"name": "u"}]}, "IAM user name .* at most 64"),
        ({"policies": [{"name": "p" * 70, "document": READ}]}, "IAM policy name .* at most 128"),
    ):
        with pytest.raises(ValueError, match=message):
            IamStack(cdk.App(), "too-long", environment=environment, props=IamProps.from_dict(config))


POLICY = {"name": "p", "document": READ}


@pytest.mark.parametrize(
    "config,message",
    [
        ({"policies": {}}, "iam.policies must be a list"),
        ({"users": None}, "iam.users must be a list"),
        ({"policies": [{"name": "p"}]}, "document is required"),
        ({"policies": [{"name": "bad/name", "document": READ}]}, "IAM policy name"),
        ({"policies": [{**POLICY, "typo": 1}]}, "unknown fields"),
        ({"policies": [{**POLICY, "description": ""}]}, "description"),
        ({"policies": [{**POLICY, "path": "app"}]}, "path"),
        ({"policies": [{**POLICY, "path": "//"}]}, "path"),
        ({"policies": [POLICY, {**POLICY, "name": "P"}]}, "duplicate policy"),
        (
            {"policies": [{"name": "p", "document": {"Statement": [{
                "Effect": "Allow", "Action": "s3:*", "Resource": "*", "Principal": "*",
            }]}}]},
            "must not specify Principal",
        ),
        (
            {"policies": [{"name": "p", "document": {"Statement": [{
                "Effect": "Allow", "Action": "s3:*", "Resource": "arn:aws:s3:::[[bucket]]",
            }]}}]},
            "unsupported placeholders: bucket",
        ),
        (
            {"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com",
                        "customer_managed_policies": ["missing"]}]},
            "unknown customer-managed policy 'missing'",
        ),
        (
            {"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com",
                        "permissions_boundary": "missing"}]},
            "unknown customer-managed policy 'missing'",
        ),
        (
            {"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com",
                        "permissions_boundary": "arn:aws:s3:::bucket"}]},
            "permissions_boundary",
        ),
        (
            {"policies": [POLICY], "roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com",
                                              "customer_managed_policies": ["p", "P"]}]},
            "duplicate customer-managed policy",
        ),
        ({"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com", "path": "x/"}]}, "path"),
        (
            {"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com",
                        "max_session_duration": 60}]},
            "max_session_duration",
        ),
        (
            {"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com",
                        "max_session_duration": True}]},
            "max_session_duration",
        ),
        (
            {"roles": [{"name": "r", "assumed_by": "ec2.amazonaws.com",
                        "force_detach_policies": True}]},
            "unknown fields",
        ),
        ({"users": [{}]}, "IAM user name"),
        ({"users": ["bad"]}, "IAM user must be a mapping"),
        ({"users": [{"name": "u", "force_destroy": True}]}, "unknown fields"),
        ({"users": [{"name": "u", "groups": "ops"}]}, "groups must be a list"),
        ({"users": [{"name": "u", "groups": ["ops", "OPS"]}]}, "duplicate group"),
        ({"users": [{"name": "u", "groups": ["bad/group"]}]}, "group name"),
        (
            {"users": [{"name": "a", "groups": ["ops"]}, {"name": "b", "groups": ["OPS"]}]},
            "differ only by case",
        ),
        ({"users": [{"name": "u"}, {"name": "U"}]}, "duplicate user"),
        ({"users": [{"name": "u", "create_access_key": "yes"}]}, "create_access_key"),
        ({"users": [{"name": "u", "secrets_manager": {}}]}, "requires create_access_key"),
        (
            {"users": [{"name": "u", "create_access_key": True,
                        "secrets_manager": {"removal_policy": "snapshot"}}]},
            "removal_policy",
        ),
        (
            {"users": [{"name": "u", "create_access_key": True,
                        "secrets_manager": {"recovery_window_in_days": 7}}]},
            "unknown fields",
        ),
        (
            {"users": [
                {"name": "a", "create_access_key": True, "secrets_manager": {"name": "keys"}},
                {"name": "b", "create_access_key": True, "secrets_manager": {"name": "keys"}},
            ]},
            "duplicate user credentials secret",
        ),
        (
            {"users": [{"name": "u", "group_policies": [
                {"group_name": "ops", "policy_name": "x", "document": READ},
            ]}]},
            "not in groups",
        ),
        ({"users": [{"name": "u", "group_policies": {}}]}, "group_policies must be a list"),
        (
            {"users": [{"name": "u", "groups": ["ops"], "group_policies": [
                {"group_name": "ops", "policy_name": "x"},
            ]}]},
            "document is required",
        ),
        (
            {"policies": [POLICY], "users": [{"name": "u", "policy": POLICY}]},
            "duplicate policy",
        ),
        ({"users": [{"name": "u", "customer_managed_policies": ["missing"]}]}, "unknown customer-managed"),
        ({"users": [{"name": "u", "managed_policies": ["arn:aws:iam::aws:policy/X"]}]}, "not ARNs"),
        ({"users": [{"name": "u", "managed_policy_arns": ["bad"]}]}, "managed_policy_arns"),
        ({"users": [{"name": "u", "notifications_email": "nope"}]}, "notifications_email"),
        ({"users": [{"name": "u", "regions": ["Mars"]}]}, "regions"),
        ({"users": [{"name": "u", "user_type": "admin"}]}, "user_type"),
    ],
)
def test_invalid_policy_and_user_config_fails_explicitly(config, message):
    with pytest.raises(ValueError, match=message):
        IamProps.from_dict(config)


@pytest.mark.parametrize(
    "statement,message",
    [
        ({"Effect": "Allow", "Action": "secretsmanager:*", "Principal": "*"}, "public Principal"),
        (
            {"Effect": "Allow", "Action": "secretsmanager:*", "Principal": {"AWS": ["*"]}},
            "public Principal",
        ),
        (
            {"Effect": "Allow", "Action": "secretsmanager:*",
             "NotPrincipal": {"AWS": "arn:aws:iam::111122223333:root"}},
            "must specify Principal",
        ),
        ({"Effect": "Allow", "Action": "secretsmanager:*"}, "must specify Principal"),
    ],
)
def test_secret_resource_policy_cannot_be_public(statement, message):
    with pytest.raises(ValueError, match=message):
        IamProps.from_dict({"users": [{
            "name": "u", "create_access_key": True,
            "secrets_manager": {"policy": {"Statement": [statement]}},
        }]})


def test_conditional_public_secret_policy_and_metadata_fields_are_accepted():
    props = IamProps.from_dict({"users": [{
        "name": "u", "create_access_key": True,
        "description": "CI user", "notifications_email": "ops@example.com",
        "regions": ["us-east-1"], "user_type": "service-linked",
        "secrets_manager": {"policy": {"Statement": [{
            "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Principal": "*",
            "Condition": {"StringEquals": {"aws:PrincipalOrgID": "o-example"}},
        }]}},
    }]})
    user = props.users[0]
    assert user.secrets_manager.policy is not None
    assert (user.notifications_email, user.regions, user.user_type) == (
        "ops@example.com", ("us-east-1",), "service-linked",
    )


def test_policy_and_user_documents_read_relative_files(write_env, tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "read.yaml").write_text(yaml.safe_dump(READ), encoding="utf-8")
    config = yaml.safe_dump({
        "policies": [{"name": "p", "document": f"{tmp_path.name}/policies/read.yaml"}],
        "users": [{
            "name": "u",
            "policy": {"name": "own", "document": f"{tmp_path.name}/policies/read.yaml"},
        }],
    })
    environment = load_environment("test", root=write_env(NETWORK, iam=config))
    assert [policy.document for policy in environment.iam.all_policies] == [READ, READ]


def test_missing_policy_file_reports_policy_and_file(write_env, tmp_path):
    config = yaml.safe_dump({"policies": [{"name": "p", "document": f"{tmp_path.name}/missing.yaml"}]})
    with pytest.raises(ValueError, match="IAM policy 'p'.*missing.yaml.*cannot read"):
        load_environment("test", root=write_env(NETWORK, iam=config))
