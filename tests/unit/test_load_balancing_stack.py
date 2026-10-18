"""Load balancing: what the config turns into, and what it refuses."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from cognitech_cdk.common.environment import load_environment
from cognitech_cdk.constructs.load_balancing import LoadBalancingProps
from cognitech_cdk.stacks.load_balancing_stack import LoadBalancingStack
from cognitech_cdk.stacks.network_stack import NetworkStack
from cognitech_cdk.stacks.security_stack import SecurityStack

NETWORK = """
name: uat
cidr_block: 10.20.0.0/16
azs: 2
private_subnets: true
public_subnet_cidrs: [10.20.0.0/24, 10.20.1.0/24]
private_subnet_cidrs: [10.20.10.0/24, 10.20.11.0/24]
"""

SECURITY_GROUPS = """
- name: alb
  description: Public load balancer
  ingress:
  - description: HTTPS from anywhere
    source: 0.0.0.0/0
    ports: 443
- name: nlb
  description: Internal network load balancer
"""

CONFIG = """
load_balancers:
- name: web
  type: application
  internal: false
  security_groups: [alb]
  access_logs:
    bucket: audit
  default_listener:
    certificate: web
- name: app
  type: network
  internal: true
  security_groups: [nlb]
  subnet_mappings:
  - subnet: primary
    private_ipv4_address: 10.20.10.10
  - subnet: secondary
target_groups:
- name: frontend
  protocol: HTTP
  port: 80
  target_type: ip
  attachments:
  - target_id: 10.20.10.5
  stickiness:
    enabled: true
    cookie_duration: 3600
  health_check:
    protocol: HTTP
    path: /health
    matcher: 200-299
- name: api
  protocol: HTTP
  port: 8080
  target_type: instance
  attachments:
  - target_id: i-0123456789abcdef0
    port: 8081
  health_check:
    path: /
    port: 8082
alb_listeners:
- name: web-https
  load_balancer: web
  protocol: HTTPS
  port: 8443
  certificate_arn: arn:aws:acm:us-east-1:111122223333:certificate/abc
  sni_certificates:
  - certificate: web
  target_group: frontend
- name: web-http
  load_balancer: web
  protocol: HTTP
  port: 80
  action: redirect
alb_listener_rules:
- name: web
  listener: web-https
  rules:
  - name: api
    priority: 10
    type: forward
    target_groups:
    - target_group: api
    conditions:
    - path_patterns: [/api/*]
      http_request_methods: [GET, POST]
  - name: canary
    priority: 20
    type: forward
    target_groups:
    - target_group: frontend
      weight: 90
    - target_group: api
      weight: 10
    conditions:
    - host_headers: [canary.example.com]
      http_headers:
      - name: X-Canary
        values: ["1"]
      query_strings:
      - key: beta
        value: "true"
nlb_listeners:
- name: app-tcp
  load_balancer: app
  protocol: TCP
  port: 3000
  target_group:
    name: backend
    protocol: TCP
    port: 3000
    target_type: ip
    preserve_client_ip: true
    stickiness:
      enabled: true
    health_check:
      protocol: TCP
"""


def _environment(write_env):
    return load_environment(
        "test", root=write_env(NETWORK, security_groups=SECURITY_GROUPS)
    )


def _build(write_env, config: str = CONFIG):
    environment = _environment(write_env)
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network = NetworkStack(app, "network", environment=environment, env=env)
    security = SecurityStack(
        app, "security", environment=environment, vpc=network.network.vpc, env=env
    )
    shared = cdk.Stack(app, "shared", env=env)
    bucket = s3.Bucket(shared, "Audit")
    certificate = acm.Certificate.from_certificate_arn(
        shared, "Web", "arn:aws:acm:us-east-1:111122223333:certificate/web"
    )
    stack = LoadBalancingStack(
        app,
        "load-balancing",
        environment=environment,
        props=LoadBalancingProps.from_dict(yaml.safe_load(config)),
        network=network.network,
        security_groups=security.security_groups.groups,
        certificates={"web": certificate},
        buckets={"audit": bucket},
        env=env,
    )
    return app, stack


def _template(write_env, config: str = CONFIG) -> Template:
    return Template.from_stack(_build(write_env, config)[1])


def _parse(config: str) -> LoadBalancingProps:
    return LoadBalancingProps.from_dict(yaml.safe_load(config))


def _lbs(template: Template) -> dict[str, dict]:
    return {
        resource["Properties"]["Name"]: resource["Properties"]
        for resource in template.find_resources(
            "AWS::ElasticLoadBalancingV2::LoadBalancer"
        ).values()
    }


def _attributes(props: dict) -> dict[str, str]:
    return {
        item["Key"]: item["Value"]
        for item in props.get("LoadBalancerAttributes", props.get("TargetGroupAttributes", []))
    }


# --- what gets built ------------------------------------------------------


def test_load_balancers_use_the_abbreviated_names(write_env):
    lbs = _lbs(_template(write_env))

    assert set(lbs) == {"intprod-use1-uat-web-alb", "intprod-use1-uat-app-nlb"}
    assert lbs["intprod-use1-uat-web-alb"]["Type"] == "application"
    assert lbs["intprod-use1-uat-web-alb"]["Scheme"] == "internet-facing"
    assert lbs["intprod-use1-uat-app-nlb"]["Type"] == "network"
    assert lbs["intprod-use1-uat-app-nlb"]["Scheme"] == "internal"


def test_alb_sits_in_public_subnets_with_only_the_configured_group(write_env):
    alb = _lbs(_template(write_env))["intprod-use1-uat-web-alb"]

    assert len(alb["Subnets"]) == 2
    assert all("Fn::ImportValue" in subnet for subnet in alb["Subnets"])
    assert len(alb["SecurityGroups"]) == 1
    assert "Fn::ImportValue" in alb["SecurityGroups"][0]


def test_alb_hardening_and_access_logs(write_env):
    attributes = _attributes(_lbs(_template(write_env))["intprod-use1-uat-web-alb"])

    assert attributes["deletion_protection.enabled"] == "false"
    assert attributes["routing.http.drop_invalid_header_fields.enabled"] == "true"
    assert attributes["access_logs.s3.enabled"] == "true"
    assert "Fn::ImportValue" in attributes["access_logs.s3.bucket"]
    assert attributes["access_logs.s3.prefix"] == "111122223333/elb-logs/application/web"


def test_access_logs_do_not_touch_the_bucket_owner(write_env):
    app, _ = _build(write_env)
    shared = Template.from_stack(app.node.find_child("shared"))

    shared.resource_count_is("AWS::S3::BucketPolicy", 0)


def test_nlb_subnet_mappings_replace_subnets(write_env):
    nlb = _lbs(_template(write_env))["intprod-use1-uat-app-nlb"]

    assert "Subnets" not in nlb
    mappings = nlb["SubnetMappings"]
    assert len(mappings) == 2
    assert mappings[0]["PrivateIPv4Address"] == "10.20.10.10"
    assert "PrivateIPv4Address" not in mappings[1]
    assert all("Fn::ImportValue" in mapping["SubnetId"] for mapping in mappings)


def test_nothing_opens_a_security_group(write_env):
    template = _template(write_env)

    template.resource_count_is("AWS::EC2::SecurityGroup", 0)
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 0)
    template.resource_count_is("AWS::EC2::SecurityGroupEgress", 0)


def test_target_groups_health_checks_and_attachments(write_env):
    template = _template(write_env)

    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 3)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "Name": "intprod-use1-uat-frontend-tg",
            "Protocol": "HTTP",
            "Port": 80,
            "TargetType": "ip",
            "Targets": [{"Id": "10.20.10.5"}],
            "HealthCheckProtocol": "HTTP",
            "HealthCheckPath": "/health",
            "Matcher": {"HttpCode": "200-299"},
            "HealthCheckIntervalSeconds": 30,
            "HealthCheckTimeoutSeconds": 10,
            "HealthyThresholdCount": 3,
            "UnhealthyThresholdCount": 3,
            "VpcId": {"Fn::ImportValue": Match.any_value()},
        },
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "Name": "intprod-use1-uat-api-tg",
            "TargetType": "instance",
            "Targets": [{"Id": "i-0123456789abcdef0", "Port": 8081}],
            "HealthCheckPort": "8082",
        },
    )


def test_stickiness_and_client_ip_attributes(write_env):
    groups = {
        props["Name"]: props
        for props in (
            resource["Properties"]
            for resource in _template(write_env)
            .find_resources("AWS::ElasticLoadBalancingV2::TargetGroup")
            .values()
        )
    }

    frontend = _attributes(groups["intprod-use1-uat-frontend-tg"])
    assert frontend["stickiness.enabled"] == "true"
    assert frontend["stickiness.type"] == "lb_cookie"
    assert frontend["stickiness.lb_cookie.duration_seconds"] == "3600"
    backend = _attributes(groups["intprod-use1-uat-backend-tg"])
    assert backend["stickiness.type"] == "source_ip"
    assert backend["preserve_client_ip.enabled"] == "true"


def test_default_listener_is_tls_with_the_keyed_certificate(write_env):
    template = _template(write_env)

    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 443,
            "Protocol": "HTTPS",
            "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
            "Certificates": [
                {"CertificateArn": "arn:aws:acm:us-east-1:111122223333:certificate/web"}
            ],
            "DefaultActions": [
                {
                    "Type": "fixed-response",
                    "FixedResponseConfig": {
                        "StatusCode": "200",
                        "ContentType": "text/plain",
                        "MessageBody": "Oops! The page you are looking for does not exist.",
                    },
                }
            ],
        },
    )


def test_https_listener_forwards_and_adds_sni(write_env):
    template = _template(write_env)

    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 8443,
            "Certificates": [
                {"CertificateArn": "arn:aws:acm:us-east-1:111122223333:certificate/abc"}
            ],
            "DefaultActions": [{"Type": "forward", "TargetGroupArn": {"Ref": Match.any_value()}}],
        },
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerCertificate",
        {"Certificates": [{"CertificateArn": "arn:aws:acm:us-east-1:111122223333:certificate/web"}]},
    )


def test_http_listener_redirects_to_https_without_tls_settings(write_env):
    listeners = [
        resource["Properties"]
        for resource in _template(write_env)
        .find_resources("AWS::ElasticLoadBalancingV2::Listener")
        .values()
        if resource["Properties"]["Port"] == 80
    ]

    assert len(listeners) == 1
    listener = dict(listeners[0])
    assert "Ref" in listener.pop("LoadBalancerArn")
    assert {"Key": "Name", "Value": "intprod-use1-uat-web-http-listener"} in listener.pop("Tags")
    assert listener == {
        "Port": 80,
        "Protocol": "HTTP",
        "DefaultActions": [
            {
                "Type": "redirect",
                "RedirectConfig": {
                    "StatusCode": "HTTP_301",
                    "Protocol": "HTTPS",
                    "Port": "443",
                },
            }
        ],
    }


def test_nlb_listener_forwards_to_its_inline_target_group(write_env):
    template = _template(write_env)

    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 3000,
            "Protocol": "TCP",
            "SslPolicy": Match.absent(),
            "DefaultActions": [{"Type": "forward", "TargetGroupArn": {"Ref": Match.string_like_regexp("Backend")}}],
        },
    )


def test_listener_rules_translate_conditions_and_weights(write_env):
    template = _template(write_env)

    template.resource_count_is("AWS::ElasticLoadBalancingV2::ListenerRule", 2)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 10,
            "Actions": [{"Type": "forward", "TargetGroupArn": Match.any_value()}],
            "Conditions": [
                {"Field": "http-request-method", "HttpRequestMethodConfig": {"Values": ["GET", "POST"]}},
                {"Field": "path-pattern", "PathPatternConfig": {"Values": ["/api/*"]}},
            ],
        },
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 20,
            "Actions": [
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [
                            {"TargetGroupArn": Match.any_value(), "Weight": 90},
                            {"TargetGroupArn": Match.any_value(), "Weight": 10},
                        ]
                    },
                }
            ],
            "Conditions": [
                {"Field": "host-header", "HostHeaderConfig": {"Values": ["canary.example.com"]}},
                {"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": "X-Canary", "Values": ["1"]}},
                {"Field": "query-string", "QueryStringConfig": {"Values": [{"Key": "beta", "Value": "true"}]}},
            ],
        },
    )


def test_public_dictionaries_are_keyed_by_config_name(write_env):
    _, stack = _build(write_env)

    assert set(stack.load_balancers) == {"web", "app"}
    assert set(stack.target_groups) == {"frontend", "api", "backend"}
    assert set(stack.listeners) == {"web-default", "web-https", "web-http", "app-tcp"}
    assert set(stack.listener_rules) == {"api", "canary"}


def test_outputs_have_stable_export_names(write_env):
    outputs = _template(write_env).find_outputs("*")
    exports = {output["Export"]["Name"] for output in outputs.values()}

    for name in ("web", "app"):
        for suffix in ("arn", "name", "dns-name", "hosted-zone-id"):
            assert f"load-balancing-{name}-lb-{suffix}" in exports
    for name in ("frontend", "api", "backend"):
        assert f"load-balancing-{name}-tg-arn" in exports
        assert f"load-balancing-{name}-tg-name" in exports
    for name in ("web-default", "web-https", "web-http", "app-tcp"):
        assert f"load-balancing-{name}-listener-arn" in exports
    assert "load-balancing-api-listener-rule-arn" in exports


def test_stack_depends_on_network_security_and_shared(write_env):
    app, stack = _build(write_env)
    app.synth()

    names = {dependency.stack_name for dependency in stack.dependencies}
    assert {"network", "security", "shared"} <= names


def test_secure_listener_falls_back_to_the_network_certificate(write_env):
    config = """
    load_balancers:
    - {name: web, type: application, internal: true, security_groups: [alb]}
    alb_listeners:
    - {name: https, load_balancer: web, protocol: HTTPS, port: 443, action: fixed-response}
    """
    environment = _environment(write_env)
    app = cdk.App()
    env = cdk.Environment(account=environment.account_id, region=environment.region)
    network = NetworkStack(app, "network", environment=environment, env=env)
    security = SecurityStack(app, "security", environment=environment, vpc=network.network.vpc, env=env)
    certificate = acm.Certificate.from_certificate_arn(
        network, "Uat", "arn:aws:acm:us-east-1:111122223333:certificate/uat"
    )
    stack = LoadBalancingStack(
        app, "lb", environment=environment, props=_parse(config), network=network.network,
        security_groups=security.security_groups.groups, certificates={"uat": certificate}, env=env,
    )
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Certificates": [{"CertificateArn": "arn:aws:acm:us-east-1:111122223333:certificate/uat"}]},
    )
    # Internal ALBs default to the private tier.
    lb = next(iter(_lbs(template).values()))
    assert lb["Scheme"] == "internal"


def test_empty_config_is_falsy():
    assert not LoadBalancingProps.from_dict(None)
    assert not LoadBalancingProps.from_dict({})
    assert LoadBalancingProps.from_dict(yaml.safe_load(CONFIG))


# --- what the config refuses ---------------------------------------------


@pytest.mark.parametrize(
    "config, message",
    [
        ("bogus: []", "unknown fields: bogus"),
        ("load_balancers:\n- {name: web, type: application, security_groups: [alb]}", "internal is required"),
        ("load_balancers:\n- {name: web, type: application, internal: true}", "need at least one security group"),
        ("load_balancers:\n- {name: web, type: gateway, internal: true}", "type must be one of"),
        ("load_balancers:\n- {name: web_1, type: network, internal: true}", "letters, digits and '-'"),
        (
            "load_balancers:\n- {name: web, type: network, internal: false, subnet_type: private}",
            "internet-facing load balancer needs public subnets",
        ),
        (
            "load_balancers:\n- {name: web, type: application, internal: true, security_groups: [a], "
            "subnet_mappings: [{subnet: primary}]}",
            "only apply to network",
        ),
        (
            "load_balancers:\n- {name: web, type: network, internal: true, default_listener: {}}",
            "default_listener only applies to application",
        ),
        (
            "load_balancers:\n- {name: web, type: network, internal: true, "
            "access_logs: {bucket: a, bucket_name: b}}",
            "exactly one of bucket or bucket_name",
        ),
        ("target_groups:\n- {name: tg, protocol: HTTP}", "protocol and port are required"),
        ("target_groups:\n- {name: tg, protocol: GENEVE, port: 6081}", "protocol must be one of"),
        (
            "target_groups:\n- {name: tg, protocol: HTTP, port: 80, attachments: [{target_id: 10.0.0.1}]}",
            "must be an EC2 instance id",
        ),
        (
            "target_groups:\n- {name: tg, protocol: HTTP, port: 80, preserve_client_ip: true}",
            "preserve_client_ip only applies to network",
        ),
        (
            "target_groups:\n- {name: tg, protocol: TCP, port: 80, health_check: {path: /}}",
            "path and matcher need an HTTP",
        ),
        (
            "target_groups:\n- {name: tg, protocol: HTTP, port: 80, health_check: {interval: 10, timeout: 10}}",
            "timeout must be shorter than interval",
        ),
        (
            "target_groups:\n- {name: tg, protocol: HTTP, port: 80, health_check: {enabled: false}}",
            "cannot be disabled",
        ),
        (
            "target_groups:\n- {name: tg, protocol: HTTP, port: 80, stickiness: {enabled: true, type: app_cookie}}",
            "cookie_name is required for app_cookie",
        ),
        (
            "alb_listeners:\n- {name: l, load_balancer: missing, protocol: HTTP, port: 80, "
            "action: fixed-response}",
            "unknown reference 'missing'",
        ),
        (
            "alb_listeners:\n- {name: l, load_balancer_arn: nope, protocol: HTTP, port: 80, action: redirect}",
            "load_balancer_arn must be a load balancer ARN",
        ),
        (
            "alb_listeners:\n- {name: l, protocol: HTTP, port: 80, action: redirect}",
            "exactly one of load_balancer or load_balancer_arn",
        ),
        (
            "alb_listeners:\n- {name: l, load_balancer: x, protocol: HTTP, port: 80, ssl_policy: p, action: redirect}",
            "ssl_policy only applies to HTTPS or TLS",
        ),
        (
            "alb_listeners:\n- {name: l, load_balancer: x, protocol: TCP, port: 80}",
            "protocol must be one of HTTP, HTTPS",
        ),
        (
            "nlb_listeners:\n- {name: l, load_balancer: x, protocol: TCP, port: 80, action: fixed-response}",
            "action must be one of forward",
        ),
        (
            "load_balancers:\n- {name: app, type: network, internal: true}\n"
            "alb_listeners:\n- {name: l, load_balancer: app, protocol: HTTP, port: 80, action: redirect}",
            "is network, not application",
        ),
        (
            "load_balancers:\n- {name: app, type: network, internal: true}\n"
            "target_groups:\n- {name: tg, protocol: HTTP, port: 80}\n"
            "nlb_listeners:\n- {name: l, load_balancer: app, protocol: TCP, port: 80, target_group: tg}",
            "network load balancer cannot forward to",
        ),
        (
            "target_groups:\n- {name: tg, protocol: HTTP, port: 80}\n- {name: TG, protocol: HTTP, port: 81}",
            "duplicate name 'TG'",
        ),
        (
            "alb_listener_rules:\n- name: g\n  listener: missing\n  rules:\n"
            "  - {name: r, priority: 1, type: fixed-response, conditions: [{path_patterns: [/]}]}",
            "unknown reference 'missing'",
        ),
        (
            "alb_listener_rules:\n- name: g\n  listener_arn: "
            "arn:aws:elasticloadbalancing:us-east-1:111122223333:listener/app/x/1/2\n  rules:\n"
            "  - {name: r, priority: 1, type: fixed-response}",
            "missing required fields: conditions",
        ),
        (
            "alb_listener_rules:\n- name: g\n  listener_arn: "
            "arn:aws:elasticloadbalancing:us-east-1:111122223333:listener/app/x/1/2\n  rules:\n"
            "  - {name: r, priority: 1, type: forward, conditions: [{source_ips: [10.0.0.0/64]}], "
            "target_groups: [{arn: arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/x/1}]}",
            "is not a CIDR",
        ),
    ],
)
def test_bad_config_is_rejected(config, message):
    with pytest.raises(ValueError, match=message):
        _parse(config)


def test_duplicate_rule_priority_on_one_listener_is_rejected():
    config = """
    load_balancers:
    - {name: web, type: application, internal: true, security_groups: [alb]}
    alb_listeners:
    - {name: http, load_balancer: web, protocol: HTTP, port: 80, action: fixed-response}
    alb_listener_rules:
    - name: g
      listener: http
      rules:
      - {name: a, priority: 5, type: fixed-response, conditions: [{path_patterns: [/a]}]}
      - {name: b, priority: 5, type: fixed-response, conditions: [{path_patterns: [/b]}]}
    """
    with pytest.raises(ValueError, match="priority 5 is already used by 'a'"):
        _parse(config)


@pytest.mark.parametrize(
    "config, message",
    [
        (
            "load_balancers:\n- {name: web, type: application, internal: false, security_groups: [nope]}",
            "security group: unknown reference 'nope'. Configured: alb, nlb",
        ),
        (
            "load_balancers:\n- {name: web, type: application, internal: false, security_groups: [alb], "
            "access_logs: {bucket: nope}}",
            "access_logs bucket: unknown reference 'nope'",
        ),
        (
            "load_balancers:\n- {name: web, type: application, internal: false, security_groups: [alb]}\n"
            "alb_listeners:\n- {name: l, load_balancer: web, protocol: HTTPS, port: 443, "
            "certificate: nope, action: fixed-response}",
            "certificate: unknown reference 'nope'",
        ),
        (
            "load_balancers:\n- {name: a-very-long-load-balancer-name, type: network, internal: true}",
            "limit is 32",
        ),
        (
            "load_balancers:\n- name: app\n  type: network\n  internal: true\n"
            "  subnet_mappings: [{subnet: primary, private_ipv4_address: 10.20.0.10}]",
            "not a usable address in the primary private subnet 10.20.10.0/24",
        ),
        (
            "load_balancers:\n- name: app\n  type: network\n  internal: true\n"
            "  subnet_mappings: [{subnet: tertiary}]",
            "no tertiary private subnet",
        ),
    ],
)
def test_bad_references_fail_at_build_time(write_env, config, message):
    with pytest.raises(ValueError, match=message):
        _build(write_env, config)
