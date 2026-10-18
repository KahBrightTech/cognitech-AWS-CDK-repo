"""OpenSearch domains: encryption, credentials, access policy and logging."""

import json

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.opensearch import OpenSearchProps
from cognitech_cdk.stacks.network_stack import NetworkStack
from cognitech_cdk.stacks.opensearch_stack import OpenSearchStack

NETWORK = "name: test\ncidr_block: 10.20.0.0/16\nazs: 2"
DOMAINS = """
source_ip_cidr: 203.0.113.10/32
domains:
- name: logs
  engine_version: OpenSearch_2.11
  cluster_config: {instance_type: t3.small.search, instance_count: 1, zone_awareness_enabled: false}
  ebs_options: {volume_size: 10}
  advanced_security_options:
    internal_user_database_enabled: true
    master_user_options: {master_user_name: admin}
"""


def _stack(write_env, text=DOMAINS):
    environment = load_environment("test", root=write_env(NETWORK))
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    app = cdk.App()
    network = NetworkStack(app, "test-network", environment=environment, env=env)
    return OpenSearchStack(
        app, "test-opensearch", environment=environment, env=env,
        props=OpenSearchProps.from_dict(yaml.safe_load(text)),
        network=network.network, security_groups={},
    )


def test_domain_is_encrypted_https_only_and_retained(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource("AWS::OpenSearchService::Domain", {
        "DeletionPolicy": "Retain",
        "Properties": Match.object_like({
            "EngineVersion": "OpenSearch_2.11",
            "EncryptionAtRestOptions": Match.object_like({"Enabled": True}),
            "NodeToNodeEncryptionOptions": {"Enabled": True},
            "DomainEndpointOptions": Match.object_like({"EnforceHTTPS": True}),
            "ClusterConfig": Match.object_like({"InstanceType": "t3.small.search", "InstanceCount": 1}),
        }),
    })
    outputs = set(template.find_outputs("*"))
    assert {"LogsDomainName", "LogsDomainArn", "LogsDomainEndpoint"} <= outputs


def test_master_password_is_generated_and_never_output(write_env):
    template = Template.from_stack(_stack(write_env))
    template.has_resource_properties("AWS::SecretsManager::Secret", {
        "GenerateSecretString": Match.object_like({"GenerateStringKey": "password"}),
    })
    domain = next(iter(template.find_resources("AWS::OpenSearchService::Domain").values()))
    master = json.dumps(domain["Properties"]["AdvancedSecurityOptions"])
    assert "resolve:secretsmanager" in master
    assert "resolve:secretsmanager" not in json.dumps(template.to_json()["Outputs"])


def test_open_access_policy_is_rejected():
    text = DOMAINS + """  access_policies:
    Version: "2012-10-17"
    Statement:
    - Effect: Allow
      Principal: {AWS: "*"}
      Action: es:*
      Resource: "*"
"""
    with pytest.raises(ValueError, match="(?i)principal|condition"):
        OpenSearchProps.from_dict(yaml.safe_load(text))


@pytest.mark.parametrize("change,message", [
    (("source_ip_cidr: 203.0.113.10/32", "source_ip_cidr: nope"), "CIDR|cidr"),
    (("ebs_options: {volume_size: 10}", "ebs_options: {volume_size: 10}\n  bogus: 1"), "unknown fields"),
])
def test_invalid_config_is_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        OpenSearchProps.from_dict(yaml.safe_load(DOMAINS.replace(*change)))


def test_empty_config_builds_nothing():
    assert not OpenSearchProps.from_dict(None)
