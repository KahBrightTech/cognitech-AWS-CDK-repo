"""Cognito user pools, clients, domains, providers and config secrets."""

import json

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_certificatemanager as acm
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.cognito import CognitoProps
from cognitech_cdk.stacks.cognito_stack import CognitoStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16"
COGNITO = """
- name: portal
  username_attributes: [email]
  mfa_configuration: OPTIONAL
  software_token_mfa_enabled: true
  domain:
    domain_name: auth.example.com
    certificate: auth
  clients:
  - name: web
    allowed_oauth_flows: [code]
    allowed_oauth_flows_user_pool_client: true
    allowed_oauth_scopes: [openid, email]
    callback_urls: [https://app.example.com/callback]
    supported_identity_providers: [COGNITO, Okta]
  identity_providers:
  - provider_name: Okta
    provider_type: SAML
    provider_details:
      MetadataURL: https://example.okta.com/app/metadata
  secret:
    create: true
    primary_client_name: web
"""


def _stack(write_env, text=COGNITO):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    source = cdk.Stack(app, "certs", env=env)
    certificate = acm.Certificate(source, "Auth", domain_name="auth.example.com")
    return CognitoStack(
        app, "test-cognito", environment=environment, env=env,
        props=CognitoProps.from_dict(yaml.safe_load(text)),
        certificates={"auth": certificate},
    )


def test_user_pool_client_provider_domain_and_outputs(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource("AWS::Cognito::UserPool", {
        "DeletionPolicy": "Retain",
        "Properties": Match.object_like({
            "UserPoolName": "int-production-use1-test-portal",
            "UsernameAttributes": ["email"],
            "MfaConfiguration": "OPTIONAL",
            "DeletionProtection": "ACTIVE",
        }),
    })
    template.has_resource_properties("AWS::Cognito::UserPoolClient", {
        "AllowedOAuthFlows": ["code"],
        "CallbackURLs": ["https://app.example.com/callback"],
        "SupportedIdentityProviders": ["COGNITO", "Okta"],
    })
    template.has_resource_properties(
        "AWS::Cognito::UserPoolIdentityProvider", {"ProviderName": "Okta", "ProviderType": "SAML"}
    )
    template.has_resource_properties("AWS::Cognito::UserPoolDomain", {
        "Domain": "auth.example.com",
        "CustomDomainConfig": {"CertificateArn": {"Fn::ImportValue": Match.any_value()}},
    })
    outputs = set(template.find_outputs("*"))
    assert {"PortalUserPoolId", "PortalUserPoolArn", "PortalClientWebId", "PortalSecretArn"} <= outputs


def test_config_secret_holds_identifiers_only(write_env):
    template = Template.from_stack(_stack(write_env))
    secret = next(iter(template.find_resources("AWS::SecretsManager::Secret").values()))
    text = json.dumps(secret["Properties"]["SecretString"])
    assert "COGNITO_PRIMARY_CLIENT_ID" in text
    assert "ClientSecret" not in json.dumps(template.to_json())


@pytest.mark.parametrize("text,message", [
    ("- name: p\n  clients:\n  - name: c\n    allowed_oauth_flows: [client_credentials]\n"
     "    allowed_oauth_flows_user_pool_client: true\n",
     "client_credentials needs generate_secret"),
    ("- name: p\n  secret: {create: true, additional_values: {DB_PASSWORD: x}}\n",
     "look like secrets"),
    ("- name: p\n  clients:\n  - name: c\n    callback_urls: [http://example.com]\n",
     "must use https"),
    ("- name: p\n  bogus: 1\n", "unknown fields"),
])
def test_invalid_config_is_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        CognitoProps.from_dict(yaml.safe_load(text))


def test_unknown_certificate_reference_fails(write_env):
    with pytest.raises(ValueError, match="unknown reference 'missing'"):
        _stack(write_env, COGNITO.replace("certificate: auth", "certificate: missing"))


def test_empty_config_builds_nothing():
    assert not CognitoProps.from_dict(None)
