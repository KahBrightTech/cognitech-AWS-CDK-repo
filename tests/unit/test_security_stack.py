"""Security groups: what the config turns into, and what it refuses."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.security import RuleProps, SecurityGroupProps
from cognitech_cdk.stacks.network_stack import NetworkStack
from cognitech_cdk.stacks.security_stack import SecurityStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: false
"""

CLUSTER = """
- name: eks-cluster
  description: EKS control plane
  ingress:
  - description: API from the nodes
    source: eks-nodes
    ports: 443
- name: eks-nodes
  description: EKS worker nodes
  ingress:
  - description: Node to node
    source: eks-nodes
    protocol: all
  - description: Kubelet from the control plane
    source: eks-cluster
    ports: 10250
  - description: NodePort range from the load balancer
    source: alb
    ports: 30000-32767
- name: alb
  description: Public load balancer
  ingress:
  - description: HTTPS from anywhere
    source: 0.0.0.0/0
    ports: 443
"""


def _template(write_env, security_groups: str) -> Template:
    environment = load_environment(
        "test", root=write_env(NETWORK, security_groups=security_groups)
    )
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network = NetworkStack(app, "network", environment=environment, env=env)
    stack = SecurityStack(
        app, "security", environment=environment, vpc=network.network.vpc, env=env
    )
    return Template.from_stack(stack)


def _groups(template: Template) -> dict[str, dict]:
    """Security groups keyed by the name they get in AWS."""
    return {
        resource["Properties"]["GroupName"]: resource["Properties"]
        for resource in template.find_resources("AWS::EC2::SecurityGroup").values()
    }


def _props(**overrides) -> dict:
    return {"peer": "10.0.0.0/8", "description": "a rule", **overrides}


# --- what gets built ------------------------------------------------------


def test_every_configured_group_is_created(write_env):
    template = _template(write_env, CLUSTER)

    assert set(_groups(template)) == {
        "int-production-use1-test-eks-cluster-sg",
        "int-production-use1-test-eks-nodes-sg",
        "int-production-use1-test-alb-sg",
    }


def test_groups_carry_the_name_tag_and_description(write_env):
    group = _groups(_template(write_env, CLUSTER))["int-production-use1-test-eks-cluster-sg"]

    assert group["GroupDescription"] == "EKS control plane"
    assert {"Key": "Name", "Value": "int-production-use1-test-eks-cluster-sg"} in group["Tags"]


def test_group_to_group_rules_are_separate_resources(write_env):
    """Inline rules between two groups would deadlock the deploy."""
    template = _template(write_env, CLUSTER)

    # cluster <- nodes, nodes <- nodes, nodes <- cluster, nodes <- alb
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 4)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {
            "IpProtocol": "tcp",
            "FromPort": 10250,
            "ToPort": 10250,
            "Description": (
                "Inbound traffic from eks cluster on tcp port 10250(kubelet) to eks nodes"
            ),
        },
    )


def test_a_cidr_rule_stays_inline_on_the_group(write_env):
    group = _groups(_template(write_env, CLUSTER))["int-production-use1-test-alb-sg"]

    assert group["SecurityGroupIngress"] == [
        {
            "CidrIp": "0.0.0.0/0",
            "Description": "Inbound traffic from 0.0.0.0/0 on tcp port 443(HTTPS) to alb",
            "FromPort": 443,
            "ToPort": 443,
            "IpProtocol": "tcp",
        }
    ]


def test_port_ranges_and_all_traffic_are_translated(write_env):
    template = _template(write_env, CLUSTER)

    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "tcp", "FromPort": 30000, "ToPort": 32767,
         "Description": "Inbound traffic from alb on tcp ports 30000-32767(NodePort) to eks nodes"},
    )
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "-1", "Description": (
            "Inbound traffic from eks nodes on all protocols and ports to eks nodes"
        )},
    )


def test_outbound_is_open_unless_egress_is_listed(write_env):
    template = _template(write_env, CLUSTER)

    for group in _groups(template).values():
        assert group["SecurityGroupEgress"] == [
            {
                "CidrIp": "0.0.0.0/0",
                "Description": (
                    "Outbound traffic to 0.0.0.0/0 on all protocols and ports"
                ),
                "IpProtocol": "-1",
            }
        ]


def test_listed_egress_replaces_the_open_default(write_env):
    template = _template(
        write_env,
        """
        - name: locked-down
          description: Only talks to the proxy
          egress:
          - description: Proxy
            destination: 10.20.0.0/16
            ports: 3128
        """,
    )

    group = _groups(template)["int-production-use1-test-locked-down-sg"]
    assert group["SecurityGroupEgress"] == [
        {
            "CidrIp": "10.20.0.0/16",
            "Description": "Outbound traffic to 10.20.0.0/16 on tcp port 3128",
            "FromPort": 3128,
            "ToPort": 3128,
            "IpProtocol": "tcp",
        }
    ]


def test_generated_descriptions_cover_udp_ipv6_prefix_lists_and_egress_groups(write_env):
    template = _template(
        write_env,
        """
        - name: app
          ingress:
          - source: "::/0"
            protocol: udp
            ports: 1000-1002
          - source: pl-0123456789abcdef0
            protocol: icmp
          egress:
          - destination: proxy
            ports: 443
            description: External API access
        - name: proxy
        """,
    )
    template.has_resource_properties("AWS::EC2::SecurityGroup", {
        "GroupName": "int-production-use1-test-app-sg",
        "SecurityGroupIngress": [{
            "CidrIpv6": "::/0", "IpProtocol": "udp", "FromPort": 1000, "ToPort": 1002,
            "Description": "Inbound traffic from ::/0 on udp ports 1000-1002 to app",
        }],
    })
    template.has_resource_properties("AWS::EC2::SecurityGroupIngress", {
        "SourcePrefixListId": "pl-0123456789abcdef0", "IpProtocol": "icmp",
        "Description": "Inbound traffic from pl-0123456789abcdef0 on icmp for all types and codes to app",
    })
    template.has_resource_properties("AWS::EC2::SecurityGroupEgress", {
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
        "Description": "Outbound traffic to proxy on tcp port 443(HTTPS)",
    })


def test_generated_rule_description_enforces_aws_length_limit(write_env):
    prefix = "Inbound traffic from 10.0.0.0/8 on tcp port 443(HTTPS) to "
    name = "x" * (255 - len(prefix))
    configuration = (
        f"- name: {name}\n  ingress:\n  - source: 10.0.0.0/8\n"
        "    ports: 443\n"
    )
    template = _template(write_env, configuration)
    rule = _groups(template)[f"int-production-use1-test-{name}-sg"]["SecurityGroupIngress"][0]
    assert len(rule["Description"]) == 255
    with pytest.raises(ValueError, match="generated ingress description exceeds 255 characters"):
        _template(write_env, configuration.replace(name, name + "x"))


def test_dev_yaml_and_aws_ingress_descriptions_match_the_requested_style(write_env):
    path = Path(__file__).resolve().parents[2] / "deployments/md-preprod/dev/env.yaml"
    groups = yaml.safe_load(path.read_text())["security_groups"]
    assert groups[0]["ingress"][0]["description"] == (
        "Inbound traffic from eks nodes on tcp port 443(HTTPS) to eks cluster"
    )
    template = _template(write_env, yaml.safe_dump(groups))
    descriptions = {
        resource["Properties"]["Description"]
        for resource in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
    }
    descriptions.update(
        rule["Description"]
        for group in _groups(template).values()
        for rule in group.get("SecurityGroupIngress", [])
    )
    assert descriptions == {
        rule["description"] for group in groups for rule in group["ingress"]
    }
    resources = template.find_resources("AWS::EC2::SecurityGroup")
    ids = {
        resource["Properties"]["GroupName"]: logical_id
        for logical_id, resource in resources.items()
    }
    nlb_id = ids["int-production-use1-test-nlb-sg"]
    nodes_id = ids["int-production-use1-test-eks-nodes-sg"]
    nlb = resources[nlb_id]["Properties"]
    assert {(rule["CidrIp"], rule["IpProtocol"], rule["FromPort"], rule["ToPort"])
            for rule in nlb["SecurityGroupIngress"]} == {
        ("0.0.0.0/0", "tcp", 80, 80), ("0.0.0.0/0", "tcp", 443, 443),
    }
    template.has_resource_properties("AWS::EC2::SecurityGroupIngress", {
        "SourceSecurityGroupId": {"Fn::GetAtt": [nlb_id, "GroupId"]},
        "GroupId": {"Fn::GetAtt": [nodes_id, "GroupId"]},
        "IpProtocol": "tcp", "FromPort": 30000, "ToPort": 32767,
        "Description": "Inbound traffic from nlb on tcp ports 30000-32767(NodePort) to eks nodes",
    })
    active = yaml.safe_load(path.read_text())
    assert active["eks_addons"][0]["load_balancer_defaults"]["security_groups"] == ["alb"]


def test_existing_ids_and_prefix_lists_are_used_as_given(write_env):
    template = _template(
        write_env,
        """
        - name: mixed
          description: Peers that live outside this stack
          ingress:
          - description: From a group built elsewhere
            source: sg-0123456789abcdef0
            ports: 443
          - description: From an AWS service
            source: pl-0123456789abcdef0
            ports: 443
        """,
    )

    # An sg- id can be written inline; a prefix list always becomes its own
    # resource, so both places have to be looked at.
    inline = _groups(template)["int-production-use1-test-mixed-sg"]["SecurityGroupIngress"]
    separate = [
        resource["Properties"]
        for resource in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
    ]
    sources = {
        rule.get("SourceSecurityGroupId") or rule.get("SourcePrefixListId")
        for rule in (*inline, *separate)
    }
    assert sources == {"sg-0123456789abcdef0", "pl-0123456789abcdef0"}


def test_extra_tags_are_applied(write_env):
    template = _template(
        write_env,
        """
        - name: eks-nodes
          description: Worker nodes
          tags:
            kubernetes.io/cluster/dev: owned
        """,
    )

    group = _groups(template)["int-production-use1-test-eks-nodes-sg"]
    assert {"Key": "kubernetes.io/cluster/dev", "Value": "owned"} in group["Tags"]


def test_each_group_is_exported(write_env):
    outputs = _template(write_env, CLUSTER).find_outputs("*")

    exports = {output["Export"]["Name"] for output in outputs.values()}
    assert exports == {
        "security-eks-cluster-sg-id",
        "security-eks-nodes-sg-id",
        "security-alb-sg-id",
    }


def test_the_stack_is_tagged_like_every_other_stack(write_env):
    template = _template(write_env, CLUSTER)

    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"Tags": Match.array_with([{"Key": "Compliance", "Value": "hippaa"}])},
    )


def test_the_groups_wait_for_the_vpc(write_env):
    """The dependency is implicit in passing the VPC object, so pin it down.

    Without it CloudFormation could try to build a group before its VPC, or
    delete the VPC while the groups still point at it. CDK only works the
    ordering out while synthesizing, so the assembly is what gets checked.
    """
    environment = load_environment(
        "test", root=write_env(NETWORK, security_groups=CLUSTER)
    )
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network = NetworkStack(app, "network", environment=environment, env=env)
    security = SecurityStack(
        app, "security", environment=environment, vpc=network.network.vpc, env=env
    )

    assembly = app.synth()
    assert network in security.dependencies
    assert "network" in [
        artifact.id for artifact in assembly.get_stack_by_name("security").dependencies
    ]

    # The VPC id has to arrive as an import, not a value copied at synth time.
    groups = Template.from_stack(security).find_resources("AWS::EC2::SecurityGroup")
    assert all(
        "Fn::ImportValue" in resource["Properties"]["VpcId"]
        for resource in groups.values()
    )


# --- what the config refuses ---------------------------------------------


def test_an_unknown_peer_is_rejected(write_env):
    with pytest.raises(ValueError, match="points at 'typo'"):
        load_environment(
            "test",
            root=write_env(
                NETWORK,
                security_groups="""
                - name: nodes
                  description: Nodes
                  ingress:
                  - description: Bad reference
                    source: typo
                    ports: 443
                """,
            ),
        )


def test_a_duplicate_group_name_is_rejected(write_env):
    with pytest.raises(ValueError, match="'nodes' is defined twice"):
        load_environment(
            "test",
            root=write_env(
                NETWORK,
                security_groups="""
                - name: nodes
                  description: Nodes
                - name: nodes
                  description: Nodes again
                """,
            ),
        )


def test_a_repeated_rule_is_rejected(write_env):
    """CDK derives a logical id from the peer and port, so a repeat would clash."""
    with pytest.raises(ValueError, match="duplicate ingress rule"):
        load_environment(
            "test",
            root=write_env(
                NETWORK,
                security_groups="""
                - name: nodes
                  description: Nodes
                  ingress:
                  - description: HTTPS
                    source: 10.0.0.0/8
                    ports: 443
                  - description: HTTPS again
                    source: 10.0.0.0/8
                    ports: 443
                """,
            ),
        )


def test_an_environment_without_security_groups_is_still_valid(write_env):
    environment = load_environment("test", root=write_env(NETWORK))

    assert environment.security_groups == ()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"protocol": "sctp"}, "protocol must be one of"),
        ({"protocol": "tcp", "ports": None}, "need 'ports'"),
        ({"protocol": "all", "ports": "443"}, "must be left out"),
        ({"protocol": "tcp", "ports": "70000"}, "outside 0-65535"),
        ({"protocol": "tcp", "ports": "443-80"}, "runs backwards"),
        ({"protocol": "tcp", "ports": "https"}, "ports must be a number"),
        ({"peer": "10.0.0.0/64", "ports": "443"}, "not a valid CIDR"),
    ],
)
def test_bad_rules_are_rejected(overrides, message):
    with pytest.raises(ValueError, match=message):
        RuleProps(**_props(**overrides))


def test_open_outbound_with_egress_rules_is_rejected():
    with pytest.raises(ValueError, match="allow_all_outbound is true"):
        SecurityGroupProps(
            name="nodes",
            description="Nodes",
            egress=(RuleProps(peer="10.0.0.0/8", description="out", ports="443"),),
            allow_all_outbound=True,
        )


def test_a_group_may_be_closed_with_no_egress_rules():
    props = SecurityGroupProps(
        name="nodes", description="Nodes", allow_all_outbound=False
    )

    assert props.allows_all_outbound is False


def test_asking_for_an_unknown_group_says_what_exists(write_env):
    environment = load_environment(
        "test", root=write_env(NETWORK, security_groups=CLUSTER)
    )
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network = NetworkStack(app, "network", environment=environment, env=env)
    stack = SecurityStack(
        app, "security", environment=environment, vpc=network.network.vpc, env=env
    )

    with pytest.raises(KeyError, match="Configured: alb, eks-cluster, eks-nodes"):
        stack.security_groups["database"]
