"""Shared parsing, document and deploy-time lookup helpers."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common import documents, schema
from cognitech_cdk.constructs.lookups import hosted_zone_id, sso_role_arn


def test_mapping_rejects_unknown_and_missing_fields():
    with pytest.raises(ValueError, match="unknown fields: extra"):
        schema.mapping({"name": "x", "extra": 1}, "thing", {"name"})
    with pytest.raises(ValueError, match="missing required fields: name"):
        schema.mapping({}, "thing", {"name"}, required=("name",))


def test_items_rejects_case_insensitive_duplicates():
    with pytest.raises(ValueError, match="duplicate name"):
        schema.items([{"name": "A"}, {"name": "a"}], "things", lambda item: item, key=lambda item: item["name"])


def test_documents_render_placeholders_without_stringifying_tokens(tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "doc.json").write_text(
        '{"Resource": "[[bucket]]", "Arn": "arn:aws:s3:::[[bucket]]/*"}', encoding="utf-8"
    )
    loaded = documents.load_document("policies/doc.json", "policy", base_dir=tmp_path)
    stack = cdk.Stack(cdk.App(), "render")
    token = stack.account
    rendered = documents.render(loaded, {"bucket": token}, "policy")
    assert rendered["Resource"] is token
    assert stack.resolve(rendered["Arn"]) == {
        "Fn::Join": ["", ["arn:aws:s3:::", {"Ref": "AWS::AccountId"}, "/*"]]
    }


@pytest.mark.parametrize("path", ["../outside.yaml", "/absolute.yaml", "https://example.com/x.yaml"])
def test_documents_must_stay_in_repository(tmp_path, path):
    with pytest.raises(ValueError, match="relative|repository"):
        documents.load_document(path, "policy", base_dir=tmp_path)


def test_unresolved_placeholders_fail():
    with pytest.raises(ValueError, match="unresolved placeholders: account"):
        documents.render({"Account": "[[account]]"}, {}, "policy")


def test_lookups_share_one_read_only_function():
    stack = cdk.Stack(cdk.App(), "lookups")
    hosted_zone_id(stack, "Zone", "example.com")
    sso_role_arn(stack, "Admin", "AdministratorAccess")
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("Custom::DeployTimeLookup", 2)
    template.has_resource_properties(
        "Custom::DeployTimeLookup", {"Kind": "HostedZone", "ZoneName": "example.com", "Private": "false"}
    )
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {"PolicyDocument": {"Statement": Match.array_with([
            Match.object_like({"Action": "route53:ListHostedZonesByName", "Resource": "*"}),
            Match.object_like({"Action": "iam:ListRoles", "Resource": "*"}),
        ])}},
    )
