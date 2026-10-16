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
    assert {role.name for role in environment.iam.roles} == {"eks-cluster", "eks-nodes"}
    assert [(profile.name, profile.role) for profile in environment.iam.instance_profiles] == [
        ("eks-nodes", "eks-nodes")
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
