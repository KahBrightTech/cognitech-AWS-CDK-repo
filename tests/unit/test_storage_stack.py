"""Private S3 buckets: parsing, safety defaults, policies, markers and replication."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.storage import BucketProps, StorageProps
from cognitech_cdk.stacks.storage_stack import StorageStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

APP_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Allow s3 access to instance role only",
            "Principal": {"AWS": "*"},
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetObject", "s3:PutObject"],
            "Resource": ["arn:aws:s3:::[[resource_name]]", "arn:aws:s3:::[[resource_name]]/*"],
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": ["us-east-1", "us-west-2"],
                    "aws:PrincipalArn": ["[[admin_role]]", "[[github_oidc_role]]"],
                }
            },
        },
        {
            "Sid": "Allow ELB service to write access logs",
            "Principal": {"AWS": "arn:aws:iam::[[account_number]]:root"},
            "Effect": "Allow",
            "Action": "s3:PutObject",
            "Resource": "arn:[[partition]]:s3:::[[bucket_name]]/ELB/*",
        },
    ],
}

APP_BUCKET = "int-production-use1-uat-app-bucket"
SOFTWARE_BUCKET = "int-production-use1-uat-software-bucket"


def _environment(write_env):
    return load_environment("test", root=write_env(NETWORK))


def _stack(write_env, raw, *, roles=None, base_dir=None) -> StorageStack:
    environment = _environment(write_env)
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    return StorageStack(
        app, "storage", environment=environment,
        props=StorageProps.from_dict(raw, base_dir=base_dir), roles=roles, env=env,
    )


def _template(write_env, raw, **kwargs) -> Template:
    return Template.from_stack(_stack(write_env, raw, **kwargs))


def _buckets(template: Template) -> dict[str, dict]:
    return {
        resource["Properties"]["BucketName"]: resource
        for resource in template.find_resources("AWS::S3::Bucket").values()
    }


def _policy_statements(template: Template) -> list[dict]:
    (policy,) = template.find_resources("AWS::S3::BucketPolicy").values()
    return policy["Properties"]["PolicyDocument"]["Statement"]


# --- parsing ------------------------------------------------------------------


def test_empty_config_creates_nothing():
    assert not StorageProps.from_dict(None)
    assert not StorageProps.from_dict([])
    assert StorageProps.from_dict([{"name": "logs"}])


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"name": "x", "extra": 1}, "unknown fields: extra"),
        ({"description": "no name"}, "missing required fields: name"),
        ({"name": "App_Bucket"}, "lowercase letters"),
        ({"name": "app", "versioning": "yes"}, "versioning must be true or false"),
        ({"name": "app", "encryption": {"enabled": False}}, "cannot be disabled"),
        ({"name": "app", "encryption": {"sse_algorithm": "DES"}}, "'AES256' or 'aws:kms'"),
        ({"name": "app", "encryption": {"sse_algorithm": "aws:kms"}}, "kms_master_key_id is required"),
        ({"name": "app", "encryption": {"bucket_key_enabled": True}}, "requires sse_algorithm 'aws:kms'"),
        (
            {"name": "app", "encryption": {"sse_algorithm": "aws:kms", "kms_master_key_id": "nope"}},
            "KMS key ARN",
        ),
        ({"name": "app", "objects": [{"key": "/abs"}]}, "must not start with '/'"),
        ({"name": "app", "objects": [{"key": "a/"}, {"key": "a/"}]}, "duplicate key"),
        ({"name": "app", "object_ownership": "acl"}, "object_ownership must be one of"),
        ({"name": "app", "removal_policy": "snapshot"}, "'retain' or 'destroy'"),
        ({"name": "app", "auto_delete_objects": True}, "requires removal_policy 'destroy'"),
        ({"name": "app", "policy_variables": {"x": "y"}}, "requires a policy"),
    ],
)
def test_invalid_bucket_config_is_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        BucketProps.from_dict(raw)


def test_duplicate_and_colliding_names_are_rejected():
    with pytest.raises(ValueError, match="duplicate name"):
        StorageProps.from_dict([{"name": "app"}, {"name": "app"}])
    with pytest.raises(ValueError, match="same CloudFormation id"):
        StorageProps.from_dict([{"name": "app-bucket"}, {"name": "appbucket"}])


def test_folder_keys_are_case_sensitive():
    props = BucketProps.from_dict({"name": "app", "objects": [{"key": "a/"}, {"key": "A/"}]})
    assert [item.key for item in props.objects] == ["a/", "A/"]


REPLICATION_RULE = {
    "status": "Enabled",
    "destination": {"bucket_arn": "arn:aws:s3:::dest-bucket"},
}


@pytest.mark.parametrize(
    ("replication", "message"),
    [
        ({"rules": [REPLICATION_RULE]}, "exactly one of role"),
        ({"role": "r", "role_arn": "arn:aws:iam::111122223333:role/r", "rules": [REPLICATION_RULE]},
         "exactly one of role"),
        ({"role_arn": "not-an-arn", "rules": [REPLICATION_RULE]}, "IAM role ARN"),
        ({"role": "r", "rules": []}, "nonempty list"),
        ({"role": "r", "rules": [{**REPLICATION_RULE, "status": "on"}]}, "'Enabled' or 'Disabled'"),
        ({"role": "r", "rules": [{"status": "Enabled", "destination": {"bucket_arn": "dest"}}]},
         "S3 bucket ARN"),
        ({"role": "r", "rules": [{"status": "Enabled", "destination": {
            "bucket_arn": "arn:aws:s3:::dest-bucket", "storage_class": "COLD"}}]}, "storage_class"),
        ({"role": "r", "rules": [{"status": "Enabled", "destination": {
            "bucket_arn": "arn:aws:s3:::dest-bucket",
            "access_control_translation": {"owner": "Destination"}}}]}, "requires account_id"),
        ({"role": "r", "rules": [{"status": "Enabled", "destination": {
            "bucket_arn": "arn:aws:s3:::dest-bucket", "replication_time": {}}}]},
         "requires replica_modification.enabled"),
        ({"role": "r", "rules": [{"status": "Enabled", "destination": {
            "bucket_arn": "arn:aws:s3:::dest-bucket", "replication_time": {"minutes": 30}}}]},
         "must be 15"),
    ],
)
def test_invalid_replication_is_rejected(replication, message):
    with pytest.raises(ValueError, match=message):
        BucketProps.from_dict({"name": "app", "replication": replication})


def test_replication_requires_versioning():
    with pytest.raises(ValueError, match="replication requires versioning"):
        BucketProps.from_dict({
            "name": "app", "versioning": False,
            "replication": {"role": "r", "rules": [REPLICATION_RULE]},
        })


# --- policy safety ----------------------------------------------------------------


def _policy(statement: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": [statement]}


PUBLIC_READ = {
    "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::[[bucket_name]]/*",
}


@pytest.mark.parametrize(
    "statement",
    [
        PUBLIC_READ,
        {**PUBLIC_READ, "Principal": {"AWS": ["*"]}},
        {**PUBLIC_READ, "Condition": {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}},
        {**PUBLIC_READ, "Condition": {"StringNotEquals": {"aws:PrincipalArn": "arn:aws:iam::1:role/x"}}},
        {**PUBLIC_READ, "Condition": {"StringLike": {"aws:PrincipalArn": "arn:aws:iam::*:role/x"}}},
        {**PUBLIC_READ, "Condition": {"StringEquals": {"aws:PrincipalArn": "*"}}},
        {**PUBLIC_READ, "Condition": {"ForAllValues:StringEquals": {"aws:PrincipalArn": "arn:aws:iam::1:role/x"}}},
        {**{k: v for k, v in PUBLIC_READ.items() if k != "Principal"},
         "NotPrincipal": {"AWS": "arn:aws:iam::111122223333:root"}},
    ],
)
def test_public_bucket_policies_are_rejected(statement):
    with pytest.raises(ValueError, match="public bucket policies|NotPrincipal"):
        BucketProps.from_dict({"name": "app", "policy": _policy(statement)})


def test_policy_variables_cannot_make_a_policy_public():
    with pytest.raises(ValueError, match="restrictive condition"):
        BucketProps.from_dict({
            "name": "app", "policy": APP_POLICY,
            "policy_variables": {"github_oidc_role": "*"},
        })


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ({"Effect": "Allow", "Action": "s3:*", "Resource": "*"}, "Principal or NotPrincipal"),
        ({"Effect": "Allow", "Principal": "*"}, "Action or NotAction"),
        ({"Effect": "Maybe", "Principal": "*", "Action": "s3:*", "Resource": "*"}, "Allow or Deny"),
        ({"Effect": "Deny", "Principal": {"Bogus": "x"}, "Action": "s3:*", "Resource": "*"}, "unknown fields"),
    ],
)
def test_malformed_policies_are_rejected(statement, message):
    with pytest.raises(ValueError, match=message):
        BucketProps.from_dict({"name": "app", "policy": _policy(statement)})


def test_unknown_policy_placeholders_and_builtin_overrides_fail():
    with pytest.raises(ValueError, match="unresolved placeholders: github_oidc_role"):
        BucketProps.from_dict({"name": "app", "policy": APP_POLICY})
    with pytest.raises(ValueError, match="built-in placeholder"):
        BucketProps.from_dict({
            "name": "app", "policy": APP_POLICY,
            "policy_variables": {"github_oidc_role": "arn:aws:iam::1:role/x", "region": "x"},
        })


def test_policy_files_must_stay_in_the_repository(tmp_path):
    with pytest.raises(ValueError, match="relative|repository"):
        BucketProps.from_dict({"name": "app", "policy": "../policy.json"}, base_dir=tmp_path)


# --- synthesized buckets -----------------------------------------------------------


def test_bucket_safety_defaults(write_env):
    template = _template(write_env, [{"name": "app-bucket", "description": "Apps"}])
    (bucket,) = _buckets(template).values()

    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    properties = bucket["Properties"]
    assert properties["BucketName"] == APP_BUCKET
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "BlockPublicPolicy": True,
        "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
    }
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
    }
    assert properties["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert {"Key": "Name", "Value": APP_BUCKET} in properties["Tags"]
    assert {"Key": "Environment", "Value": "test"} in properties["Tags"]

    (statement,) = _policy_statements(template)
    assert statement["Effect"] == "Deny"
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    template.resource_count_is("Custom::DeployTimeLookup", 0)
    template.resource_count_is("Custom::S3Object", 0)


def test_kms_encryption_and_options(write_env):
    key = "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
    template = _template(write_env, [{
        "name": "data",
        "versioning": False,
        "encryption": {"sse_algorithm": "aws:kms", "kms_master_key_id": key, "bucket_key_enabled": True},
        "object_ownership": "bucket_owner_preferred",
        "removal_policy": "destroy",
    }])
    (bucket,) = _buckets(template).values()
    properties = bucket["Properties"]
    assert bucket["DeletionPolicy"] == "Delete"
    assert "VersioningConfiguration" not in properties
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [{
            "BucketKeyEnabled": True,
            "ServerSideEncryptionByDefault": {"KMSMasterKeyID": key, "SSEAlgorithm": "aws:kms"},
        }]
    }
    assert properties["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]}


def test_kms_key_id_and_alias_are_resolved_in_this_account(write_env):
    template = _template(write_env, [
        {"name": "by-id", "encryption": {"sse_algorithm": "aws:kms", "kms_master_key_id": "mrk-" + "a" * 32}},
        {"name": "by-alias", "encryption": {"sse_algorithm": "aws:kms", "kms_master_key_id": "alias/data"}},
    ])
    buckets = _buckets(template)

    def key_of(name):
        rule = buckets[name]["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        return json.dumps(rule["ServerSideEncryptionByDefault"]["KMSMasterKeyID"])

    assert "111122223333:key/mrk-" in key_of("int-production-use1-uat-by-id")
    assert "111122223333:alias/data" in key_of("int-production-use1-uat-by-alias")


def test_final_bucket_name_must_fit_s3_limits(write_env):
    with pytest.raises(ValueError, match="3-63"):
        _stack(write_env, [{"name": "x" * 50}])


def test_policy_file_placeholders_and_lazy_sso_lookups(write_env, tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "app.json").write_text(json.dumps(APP_POLICY), encoding="utf-8")
    oidc = "arn:aws:iam::111122223333:role/prod-OIDCGitHubRole-role"
    template = _template(
        write_env,
        [{
            "name": "app-bucket",
            "policy": "policies/app.json",
            "policy_variables": {"github_oidc_role": oidc},
        }],
        base_dir=tmp_path,
    )

    statements = _policy_statements(template)
    assert [statement.get("Sid") for statement in statements] == [
        None, "Allow s3 access to instance role only", "Allow ELB service to write access logs",
    ]
    access, elb = statements[1], statements[2]
    assert access["Principal"] == {"AWS": "*"}
    assert access["Resource"] == [f"arn:aws:s3:::{APP_BUCKET}", f"arn:aws:s3:::{APP_BUCKET}/*"]
    principals = access["Condition"]["StringEquals"]["aws:PrincipalArn"]
    lookup, attribute = principals[0]["Fn::GetAtt"]
    assert "AdminRoleLookup" in lookup and attribute == "Value"
    assert principals[1] == oidc
    assert elb["Principal"] == {"AWS": "arn:aws:iam::111122223333:root"}
    assert elb["Resource"] == {
        "Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, f":s3:::{APP_BUCKET}/ELB/*"]]
    }

    template.resource_count_is("Custom::DeployTimeLookup", 1)
    template.has_resource_properties(
        "Custom::DeployTimeLookup", {"Kind": "SsoRole", "PermissionSet": "AdministratorAccess"}
    )


def test_network_role_lookup_is_shared_between_buckets(write_env):
    policy = _policy({
        "Effect": "Allow", "Principal": {"AWS": "[[network_role]]"},
        "Action": "s3:GetObject", "Resource": "arn:aws:s3:::[[resource_name]]/*",
    })
    template = _template(write_env, [
        {"name": "one", "policy": policy},
        {"name": "two", "policy": policy},
    ])
    template.resource_count_is("Custom::DeployTimeLookup", 1)
    template.has_resource_properties(
        "Custom::DeployTimeLookup", {"Kind": "SsoRole", "PermissionSet": "NetworkAdministrator"}
    )


def test_folder_markers_are_s3_objects_without_bucket_deployment(write_env):
    template = _template(write_env, [{"name": "software-bucket", "objects": [{"key": "Ansible_Tower/"}]}])

    template.resource_count_is("Custom::CDKBucketDeployment", 0)
    (marker,) = template.find_resources("Custom::S3Object").values()
    create = json.dumps(marker["Properties"]["Create"])
    assert '\\"action\\":\\"putObject\\"' in create
    assert '\\"Key\\":\\"Ansible_Tower/\\"' in create
    assert f"{SOFTWARE_BUCKET}/Ansible_Tower/" in create
    assert "Delete" not in marker["Properties"]  # retained bucket keeps its marker
    assert marker["Properties"]["InstallLatestAwsSdk"] is False
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {"Statement": [Match.object_like({
            "Action": ["s3:PutObject", "s3:DeleteObject"],
            "Resource": {"Fn::Join": ["", [
                {"Fn::GetAtt": [Match.any_value(), "Arn"]}, "/Ansible_Tower/",
            ]]},
        })]},
    })


def test_destroyed_buckets_remove_markers_and_can_auto_delete(write_env):
    template = _template(write_env, [{
        "name": "scratch", "removal_policy": "destroy", "auto_delete_objects": True,
        "objects": [{"key": "tmp/"}],
    }])
    (marker,) = template.find_resources("Custom::S3Object").values()
    assert "deleteObject" in json.dumps(marker["Properties"]["Delete"])
    template.resource_count_is("Custom::S3AutoDeleteObjects", 1)


def test_replication_uses_a_configured_role(write_env):
    environment = _environment(write_env)
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    iam_stack = cdk.Stack(app, "iam", env=env)
    role = iam.Role(
        iam_stack, "Replication", role_name="replication-role",
        assumed_by=iam.ServicePrincipal("s3.amazonaws.com"),
    )
    replica_key = "arn:aws:kms:us-west-2:444455556666:key/11111111-2222-3333-4444-555555555555"
    props = StorageProps.from_dict([{
        "name": "source",
        "replication": {
            "role": "s3-replication",
            "rules": [
                {
                    "status": "Enabled",
                    "prefix": "logs/",
                    "delete_marker_replication": True,
                    "destination": {
                        "bucket_arn": "arn:aws:s3:::dest-bucket",
                        "storage_class": "STANDARD_IA",
                        "account_id": 444455556666,
                        "access_control_translation": {"owner": "Destination"},
                        "encryption_configuration": {"replica_kms_key_id": replica_key},
                        "replication_time": {"minutes": 15},
                        "replica_modification": {"enabled": True},
                    },
                },
                REPLICATION_RULE,
            ],
        },
    }])
    stack = StorageStack(
        app, "storage", environment=environment, props=props,
        roles={"s3-replication": role}, env=env,
    )
    (bucket,) = _buckets(Template.from_stack(stack)).values()
    configuration = bucket["Properties"]["ReplicationConfiguration"]

    # The role lives in another stack, so its ARN arrives as a cross-stack import.
    assert set(configuration["Role"]) == {"Fn::ImportValue"}
    first, second = configuration["Rules"]
    assert first == {
        "Id": "replication-rule-0",
        "Priority": 0,
        "Status": "Enabled",
        "Filter": {"Prefix": "logs/"},
        "DeleteMarkerReplication": {"Status": "Enabled"},
        "Destination": {
            "Bucket": "arn:aws:s3:::dest-bucket",
            "StorageClass": "STANDARD_IA",
            "Account": "444455556666",
            "AccessControlTranslation": {"Owner": "Destination"},
            "EncryptionConfiguration": {"ReplicaKmsKeyID": replica_key},
            "ReplicationTime": {"Status": "Enabled", "Time": {"Minutes": 15}},
            "Metrics": {"Status": "Enabled", "EventThreshold": {"Minutes": 15}},
        },
        "SourceSelectionCriteria": {"SseKmsEncryptedObjects": {"Status": "Enabled"}},
    }
    assert second == {
        "Id": "replication-rule-1",
        "Priority": 1,
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "DeleteMarkerReplication": {"Status": "Disabled"},
        "Destination": {"Bucket": "arn:aws:s3:::dest-bucket", "StorageClass": "STANDARD"},
    }


def test_replication_role_arn_and_missing_role_reference(write_env):
    arn = "arn:aws:iam::111122223333:role/replication"
    template = _template(write_env, [{
        "name": "source", "replication": {"role_arn": arn, "rules": [REPLICATION_RULE]},
    }])
    (bucket,) = _buckets(template).values()
    assert bucket["Properties"]["ReplicationConfiguration"]["Role"] == arn

    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _stack(write_env, [{"name": "source", "replication": {"role": "missing", "rules": [REPLICATION_RULE]}}])


def test_outputs_export_bucket_name_and_arn(write_env):
    stack = _stack(write_env, [
        {"name": "app-bucket", "description": "The application bucket"},
        {"name": "audit-bucket"},
    ])
    template = Template.from_stack(stack)

    assert stack.config.network.name == "uat"
    artifact = cdk.App.of(stack).synth().get_stack_by_name("storage")
    assert artifact.environment.account == "111122223333"
    assert set(stack.buckets) == {"app-bucket", "audit-bucket"}
    template.has_output("AppBucketBucketName", {
        "Value": {"Ref": Match.any_value()},
        "Description": "The application bucket",
        "Export": {"Name": "storage-app-bucket-bucket-name"},
    })
    template.has_output("AppBucketBucketArn", {
        "Value": {"Fn::GetAtt": [Match.any_value(), "Arn"]},
        "Export": {"Name": "storage-app-bucket-bucket-arn"},
    })
    template.has_output("AuditBucketBucketName", {"Export": {"Name": "storage-audit-bucket-bucket-name"}})
    template.has_output("AuditBucketBucketArn", {"Export": {"Name": "storage-audit-bucket-bucket-arn"}})
