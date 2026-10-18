"""ACM public certificates validated through Route 53."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.certificates import CertificatesProps
from cognitech_cdk.stacks.certificates_stack import CertificatesStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

UAT = {
    "name": "uat",
    "domain_name": "*.uat.example.com",
    "validation_method": "DNS",
    "zone_name": "example.com",
}


def _stack(write_env, raw) -> tuple[CertificatesStack, Template]:
    environment = load_environment("test", root=write_env(NETWORK))
    app = cdk.App()
    stack = CertificatesStack(
        app, "certs", environment=environment, props=CertificatesProps.from_dict(raw),
        env=cdk.Environment(account=environment.account_id, region=environment.region),
    )
    return stack, Template.from_stack(stack)


# --- parsing ---------------------------------------------------------------


def test_none_is_empty():
    assert not CertificatesProps.from_dict(None)


def test_dns_needs_a_zone_name():
    with pytest.raises(ValueError, match="DNS validation requires zone_name"):
        CertificatesProps.from_dict([{"name": "a", "domain_name": "a.example.com"}])


@pytest.mark.parametrize(("field", "value", "message"), [
    ("validation_method", "HTTP", "validation_method must be one of"),
    ("domain_name", "not a domain", "must be a domain name"),
    ("hosted_zone_id", "zone-1", "hosted_zone_id must look like"),
    ("extra", 1, "unknown fields: extra"),
])
def test_invalid_fields(field, value, message):
    with pytest.raises(ValueError, match=message):
        CertificatesProps.from_dict([{**UAT, field: value}])


def test_metadata_settings_need_enabled():
    with pytest.raises(ValueError, match="settings require enabled: true"):
        CertificatesProps.from_dict([{**UAT, "secrets_manager": {"name": "x"}}])


# --- synthesized resources -------------------------------------------------


def test_zone_name_resolves_at_deploy_time(write_env):
    stack, template = _stack(write_env, [UAT])

    template.has_resource_properties("Custom::DeployTimeLookup", {
        "Kind": "HostedZone", "ZoneName": "example.com", "Private": "false",
    })
    lookup_id = next(iter(template.find_resources("Custom::DeployTimeLookup")))
    template.has_resource_properties("AWS::CertificateManager::Certificate", {
        "DomainName": "*.uat.example.com",
        "ValidationMethod": "DNS",
        "DomainValidationOptions": [{
            "DomainName": "*.uat.example.com",
            "HostedZoneId": {"Fn::GetAtt": [lookup_id, "Value"]},
        }],
        "Tags": Match.array_with([{"Key": "Name", "Value": "int-production-use1-uat-uat"}]),
    })
    assert set(stack.certificates) == {"uat"}
    outputs = template.find_outputs("*")
    assert outputs["UatCertificateArn"]["Export"]["Name"] == "certs-uat-certificate-arn"
    assert outputs["UatCertificateDomainName"]["Value"] == "*.uat.example.com"
    assert outputs["UatCertificateDomainName"]["Export"]["Name"] == "certs-uat-certificate-domain-name"


def test_one_lookup_per_zone_and_explicit_ids_skip_it(write_env):
    _, template = _stack(write_env, [
        UAT,
        {**UAT, "name": "api", "domain_name": "api.example.com",
         "subject_alternative_names": ["www.example.com"]},
        {**UAT, "name": "other", "domain_name": "other.example.org", "zone_name": "example.org",
         "hosted_zone_id": "Z0123456789ABC"},
    ])
    template.resource_count_is("Custom::DeployTimeLookup", 1)
    template.resource_count_is("AWS::CertificateManager::Certificate", 3)
    template.has_resource_properties("AWS::CertificateManager::Certificate", {
        "DomainName": "api.example.com",
        "SubjectAlternativeNames": ["www.example.com"],
    })
    template.has_resource_properties("AWS::CertificateManager::Certificate", {
        "DomainName": "other.example.org",
        "DomainValidationOptions": [{"DomainName": "other.example.org", "HostedZoneId": "Z0123456789ABC"}],
    })


def test_email_validation(write_env):
    _, template = _stack(write_env, [
        {"name": "mail", "domain_name": "mail.example.com", "validation_method": "EMAIL"},
    ])
    template.has_resource_properties("AWS::CertificateManager::Certificate", {"ValidationMethod": "EMAIL"})
    template.resource_count_is("Custom::DeployTimeLookup", 0)


def test_optional_metadata_secret_holds_no_key_material(write_env):
    stack, template = _stack(write_env, [{**UAT, "secrets_manager": {"enabled": True}}])
    template.has_resource("AWS::SecretsManager::Secret", {
        "DeletionPolicy": "Retain",
        "Properties": {
            "Name": "int-production-use1-uat-uat-acm-cert",
            "Description": "Metadata for ACM public certificate",
        },
    })
    secret = next(iter(template.find_resources("AWS::SecretsManager::Secret").values()))
    rendered = json.dumps(secret["Properties"]["SecretString"])
    assert "certificate_arn" in rendered and 'private_key_exportable\\":\\"false' in rendered
    assert "PRIVATE KEY" not in rendered
    assert set(stack.secrets) == {"uat"}
    outputs = template.find_outputs("*")
    assert outputs["UatCertificateSecretArn"]["Export"]["Name"] == "certs-uat-certificate-secret-arn"
