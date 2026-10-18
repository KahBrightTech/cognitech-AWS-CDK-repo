"""Load balancers, target groups, listeners and listener rules.

Mirrors the Terraform `Load-Balancers`, `Target-groups`, `alb-listeners`,
`alb-listener-rule` and `nlb-listener` modules from one YAML mapping:

    load_balancing:
      load_balancers:
      - name: web
        type: application          # application | network
        internal: false            # required, so exposure is always a decision
        subnet_type: public        # public | private (default: private when internal)
        security_groups: [alb]     # names from security_groups; ALBs need one
        access_logs:
          bucket: audit            # key in `buckets`, or `bucket_name: literal`
        default_listener:          # optional ALB catch-all, HTTPS 443 fixed-response
          certificate: web         # key in `certificates`, or `certificate_arn`
      target_groups:
      - name: frontend
        protocol: HTTP
        port: 80
        target_type: ip
        health_check: {path: /, matcher: 200-299}
      alb_listeners:
      - name: web-https
        load_balancer: web         # or load_balancer_arn
        protocol: HTTPS
        port: 443
        certificate: web
        target_group: frontend     # a key, an inline mapping, or target_group_arn
      alb_listener_rules:
      - name: web
        listener: web-https        # or listener_arn
        rules:
        - name: api
          priority: 10
          type: forward
          target_groups: [{target_group: frontend}]
          conditions: [{path_patterns: [/api/*]}]
      nlb_listeners:
      - name: app
        load_balancer: app
        protocol: TCP
        port: 3000
        target_group: backend

Listeners are CloudFormation-level resources, so nothing here adds security
group rules: a load balancer only accepts what its configured groups allow.

Load balancer and target group names have a 32 character AWS limit, so they
use the abbreviated form `{account_name_abr}-{region_prefix}-{network}-{name}-{alb|nlb|tg}`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as targets
from aws_cdk import aws_s3 as s3
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.config import ORDINALS, Common
from cognitech_cdk.constructs.network import Network

LB_TYPES = ("application", "network")
ALB_PROTOCOLS = ("HTTP", "HTTPS")
NLB_PROTOCOLS = ("TCP", "TLS", "UDP", "TCP_UDP")
SECURE_PROTOCOLS = ("HTTPS", "TLS")
TARGET_TYPES = ("instance", "ip")
ACTIONS = ("forward", "fixed-response", "redirect")
CONTENT_TYPES = (
    "text/plain", "text/css", "text/html", "application/javascript", "application/json",
)
# AWS's own default (ELBSecurityPolicy-2016-08) still accepts TLS 1.0, so the
# CDK port asks for TLS 1.2+ unless the config names a policy.
DEFAULT_SSL_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"
MAX_NAME = 32
MAX_FORWARD_TARGETS = 5

LB_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
INSTANCE_ID = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})")
HTTP_METHOD = re.compile(r"[A-Z_-]{1,40}")
STATUS_CODE = re.compile(r"[245]\d\d")
MATCHER = re.compile(r"\d{3}(?:-\d{3})?(?:,\d{3}(?:-\d{3})?)*")
ARN = {
    "load balancer": re.compile(r"arn:aws[a-z-]*:elasticloadbalancing:[a-z0-9-]+:\d{12}:loadbalancer/.+"),
    "target group": re.compile(r"arn:aws[a-z-]*:elasticloadbalancing:[a-z0-9-]+:\d{12}:targetgroup/.+"),
    "listener": re.compile(r"arn:aws[a-z-]*:elasticloadbalancing:[a-z0-9-]+:\d{12}:listener/.+"),
    "certificate": re.compile(r"arn:aws[a-z-]*:(?:acm|iam):[a-z0-9-]*:\d{12}:(?:certificate|server-certificate)/.+"),
}


def construct_id(name: str) -> str:
    """`ecs-web` -> `EcsWeb`, so logical ids stay readable."""
    return "".join(part.capitalize() for part in re.split(r"[-_\s]+", name) if part)


def _lb_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not LB_NAME.fullmatch(value):
        raise ValueError(
            f"{label} must use letters, digits and '-' only, and not start or end "
            "with '-' (AWS load balancer and target group naming)."
        )
    return value


def _choice(
    value: Any, label: str, choices: tuple[str, ...], *, default: str | None = None,
    upper: bool = False,
) -> str | None:
    if value is None:
        return default
    text = value.upper() if upper and isinstance(value, str) else value
    if text not in choices:
        raise ValueError(f"{label} must be one of {', '.join(choices)}, got {value!r}.")
    return text


def _port(value: Any, label: str, *, default: int | None = None) -> int | None:
    return schema.integer(value, label, default=default, minimum=1, maximum=65535)


def _arn(value: Any, label: str, kind: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not ARN[kind].fullmatch(value):
        raise ValueError(f"{label} must be a {kind} ARN.")
    return value


def _one_of(data: Mapping[str, Any], label: str, *keys: str, required: bool = True) -> None:
    given = [key for key in keys if data.get(key) is not None]
    if len(given) > 1 or (required and not given):
        raise ValueError(f"{label}: provide exactly one of {' or '.join(keys)}.")


def _list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return value


def _unique(entries: Any, label: str) -> None:
    seen: set[str] = set()
    for entry in entries:
        key = entry.name.casefold()
        if key in seen:
            raise ValueError(f"{label}: duplicate name '{entry.name}'.")
        seen.add(key)


# --- target groups ---------------------------------------------------------


@dataclass(frozen=True)
class AttachmentProps:
    target_id: str
    port: int | None = None


@dataclass(frozen=True)
class StickinessProps:
    enabled: bool
    type: str
    cookie_duration: int | None = None
    cookie_name: str | None = None


@dataclass(frozen=True)
class HealthCheckProps:
    protocol: str | None = None
    port: int | None = None
    path: str | None = None
    matcher: str | None = None
    interval: int = 30
    timeout: int = 10
    healthy_threshold: int = 3
    unhealthy_threshold: int = 3


@dataclass(frozen=True)
class TargetGroupProps:
    name: str
    protocol: str
    port: int
    target_type: str = "instance"
    preserve_client_ip: bool | None = None
    attachments: tuple[AttachmentProps, ...] = ()
    stickiness: StickinessProps | None = None
    health_check: HealthCheckProps | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def family(self) -> str:
        return "application" if self.protocol in ALB_PROTOCOLS else "network"

    @classmethod
    def from_dict(cls, raw: Any, *, label: str = "Target group", default_name: str | None = None) -> TargetGroupProps:
        data = schema.mapping(raw, label, {
            "name", "protocol", "port", "target_type", "preserve_client_ip",
            "attachments", "stickiness", "health_check", "tags",
        }, required=() if default_name else ("name",))
        name = _lb_name(data.get("name", default_name), f"{label} name")
        label = f"Target group '{name}'"
        if data.get("protocol") is None or data.get("port") is None:
            raise ValueError(f"{label}: protocol and port are required.")
        protocol = _choice(data["protocol"], f"{label}: protocol", ALB_PROTOCOLS + NLB_PROTOCOLS, upper=True)
        port = _port(data["port"], f"{label}: port")
        family = "application" if protocol in ALB_PROTOCOLS else "network"
        target_type = _choice(data.get("target_type"), f"{label}: target_type", TARGET_TYPES, default="instance")
        preserve = data.get("preserve_client_ip")
        if preserve is not None:
            if family != "network":
                raise ValueError(f"{label}: preserve_client_ip only applies to network target groups.")
            preserve = schema.boolean(preserve, f"{label}: preserve_client_ip")

        attachments = []
        for index, item in enumerate(_list(data.get("attachments"), f"{label}: attachments")):
            where = f"{label}, attachment {index + 1}"
            entry = schema.mapping(item, where, {"target_id", "port"}, required=("target_id",))
            target_id = schema.required_string(entry["target_id"], f"{where}: target_id")
            if target_type == "instance" and not INSTANCE_ID.fullmatch(target_id):
                raise ValueError(f"{where}: target_id must be an EC2 instance id for instance targets.")
            if target_type == "ip":
                try:
                    ipaddress.ip_address(target_id)
                except ValueError:
                    raise ValueError(f"{where}: target_id must be an IP address for ip targets.") from None
            attachments.append(AttachmentProps(target_id, _port(entry.get("port"), f"{where}: port")))

        stickiness = None
        if data.get("stickiness") is not None:
            where = f"{label}: stickiness"
            entry = schema.mapping(
                data["stickiness"], where, {"enabled", "type", "cookie_duration", "cookie_name"},
                required=("enabled",),
            )
            kinds = ("lb_cookie", "app_cookie") if family == "application" else ("source_ip",)
            kind = _choice(entry.get("type"), f"{where} type", kinds, default=kinds[0])
            duration = schema.integer(entry.get("cookie_duration"), f"{where} cookie_duration", minimum=1, maximum=604800)
            cookie = schema.string(entry.get("cookie_name"), f"{where} cookie_name")
            if family == "network" and duration is not None:
                raise ValueError(f"{where}: cookie_duration only applies to application target groups.")
            if (kind == "app_cookie") != (cookie is not None):
                raise ValueError(f"{where}: cookie_name is required for app_cookie and only valid there.")
            stickiness = StickinessProps(schema.boolean(entry["enabled"], f"{where} enabled"), kind, duration, cookie)

        health_check = None
        if data.get("health_check") is not None:
            health_check = _health_check(data["health_check"], label, protocol, family)

        return cls(
            name=name, protocol=protocol, port=port, target_type=target_type,
            preserve_client_ip=preserve, attachments=tuple(attachments),
            stickiness=stickiness, health_check=health_check,
            tags=schema.string_map(data.get("tags"), f"{label}: tags"),
        )


def _health_check(raw: Any, label: str, protocol: str, family: str) -> HealthCheckProps:
    where = f"{label}: health_check"
    data = schema.mapping(raw, where, {
        "enabled", "protocol", "port", "path", "matcher", "interval", "timeout",
        "healthy_threshold", "unhealthy_threshold",
    })
    if not schema.boolean(data.get("enabled"), f"{where} enabled", default=True):
        raise ValueError(
            f"{where}: health checks cannot be disabled for instance or ip targets."
        )
    allowed = ALB_PROTOCOLS if family == "application" else ("TCP",) + ALB_PROTOCOLS
    check_protocol = _choice(data.get("protocol"), f"{where} protocol", allowed, upper=True)
    effective = check_protocol or (protocol if protocol in ALB_PROTOCOLS else "TCP")
    path = schema.string(data.get("path"), f"{where} path")
    matcher = data.get("matcher")
    if matcher is not None:
        matcher = str(matcher)
        if not MATCHER.fullmatch(matcher):
            raise ValueError(f"{where}: matcher must be HTTP codes like 200 or 200-299.")
    if effective not in ALB_PROTOCOLS and (path is not None or matcher is not None):
        raise ValueError(f"{where}: path and matcher need an HTTP or HTTPS health check.")
    if path is not None and not path.startswith("/"):
        raise ValueError(f"{where}: path must start with '/'.")
    interval = schema.integer(data.get("interval"), f"{where} interval", default=30, minimum=5, maximum=300)
    timeout = schema.integer(data.get("timeout"), f"{where} timeout", default=10, minimum=2, maximum=120)
    if timeout >= interval:
        raise ValueError(f"{where}: timeout must be shorter than interval.")
    healthy = schema.integer(data.get("healthy_threshold"), f"{where} healthy_threshold", default=3, minimum=2, maximum=10)
    unhealthy = schema.integer(data.get("unhealthy_threshold"), f"{where} unhealthy_threshold", default=3, minimum=2, maximum=10)
    return HealthCheckProps(
        protocol=check_protocol, port=_port(data.get("port"), f"{where} port"),
        path=path, matcher=matcher, interval=interval, timeout=timeout,
        healthy_threshold=healthy, unhealthy_threshold=unhealthy,
    )


# --- actions ---------------------------------------------------------------


@dataclass(frozen=True)
class ForwardTargetProps:
    target_group: str | None = None
    arn: str | None = None
    weight: int | None = None


@dataclass(frozen=True)
class FixedResponseProps:
    content_type: str = "text/plain"
    message_body: str = "Oops! The page you are looking for does not exist."
    status_code: str = "200"


@dataclass(frozen=True)
class RedirectProps:
    protocol: str = "HTTPS"
    port: str = "443"
    status_code: str = "HTTP_301"
    host: str | None = None
    path: str | None = None
    query: str | None = None


@dataclass(frozen=True)
class ActionProps:
    type: str
    targets: tuple[ForwardTargetProps, ...] = ()
    fixed_response: FixedResponseProps | None = None
    redirect: RedirectProps | None = None


def _fixed_response(raw: Any, label: str) -> FixedResponseProps:
    data = schema.mapping(raw or {}, f"{label}: fixed_response", {"content_type", "message_body", "status_code"})
    defaults = FixedResponseProps()
    status = str(data.get("status_code", defaults.status_code))
    if not STATUS_CODE.fullmatch(status):
        raise ValueError(f"{label}: fixed_response status_code must be a 2XX, 4XX or 5XX code.")
    body = schema.string(data.get("message_body"), f"{label}: fixed_response message_body", default=defaults.message_body, allow_empty=True)
    if len(body) > 1024:
        raise ValueError(f"{label}: fixed_response message_body is limited to 1024 characters.")
    return FixedResponseProps(
        content_type=_choice(data.get("content_type"), f"{label}: fixed_response content_type", CONTENT_TYPES, default=defaults.content_type),
        message_body=body,
        status_code=status,
    )


def _redirect(raw: Any, label: str) -> RedirectProps:
    where = f"{label}: redirect"
    data = schema.mapping(raw or {}, where, {"protocol", "port", "status_code", "host", "path", "query"})
    port = data.get("port", "443")
    if str(port) != "#{port}":
        _port(int(port) if str(port).isdigit() else port, f"{where} port")
    return RedirectProps(
        protocol=_choice(data.get("protocol"), f"{where} protocol", ("HTTP", "HTTPS", "#{protocol}"), default="HTTPS"),
        port=str(port),
        status_code=_choice(data.get("status_code"), f"{where} status_code", ("HTTP_301", "HTTP_302"), default="HTTP_301"),
        host=schema.string(data.get("host"), f"{where} host"),
        path=schema.string(data.get("path"), f"{where} path"),
        query=schema.string(data.get("query"), f"{where} query", allow_empty=True),
    )


# --- listeners -------------------------------------------------------------


@dataclass(frozen=True)
class CertificateRef:
    certificate: str | None = None
    arn: str | None = None


@dataclass(frozen=True)
class ListenerProps:
    name: str
    kind: str  # "application" or "network"
    port: int
    protocol: str
    action: ActionProps
    load_balancer: str | None = None
    load_balancer_arn: str | None = None
    ssl_policy: str | None = None
    certificate: CertificateRef | None = None
    sni_certificates: tuple[CertificateRef, ...] = ()
    inline_target_group: TargetGroupProps | None = None

    @property
    def secure(self) -> bool:
        return self.protocol in SECURE_PROTOCOLS


def _certificate_ref(data: Mapping[str, Any], label: str, *, required: bool) -> CertificateRef | None:
    _one_of(data, label, "certificate", "certificate_arn", required=required)
    if data.get("certificate") is None and data.get("certificate_arn") is None:
        return None
    return CertificateRef(
        certificate=schema.name(data["certificate"], f"{label}: certificate") if data.get("certificate") is not None else None,
        arn=_arn(data.get("certificate_arn"), f"{label}: certificate_arn", "certificate"),
    )


LISTENER_FIELDS = {
    "name", "load_balancer", "load_balancer_arn", "port", "protocol", "ssl_policy",
    "certificate", "certificate_arn", "sni_certificates", "action", "target_group",
    "target_group_arn",
}


def _listener(raw: Any, kind: str, label: str, *, default_name: str | None = None) -> ListenerProps:
    keys = LISTENER_FIELDS | ({"fixed_response", "redirect"} if kind == "application" else set())
    data = schema.mapping(raw, label, keys, required=("port", "protocol") if default_name else ("name", "port", "protocol"))
    name = schema.name(data.get("name", default_name), f"{label} name")
    label = f"{'ALB' if kind == 'application' else 'NLB'} listener '{name}'"
    if default_name is None:
        _one_of(data, label, "load_balancer", "load_balancer_arn")
    protocols = ALB_PROTOCOLS if kind == "application" else NLB_PROTOCOLS
    protocol = _choice(data["protocol"], f"{label}: protocol", protocols, upper=True)
    port = _port(data["port"], f"{label}: port")
    secure = protocol in SECURE_PROTOCOLS

    certificate = None
    sni: list[CertificateRef] = []
    ssl_policy = schema.string(data.get("ssl_policy"), f"{label}: ssl_policy")
    if secure:
        # Missing certificates fall back to the network-named certificate at
        # build time, as the Terraform formation does.
        certificate = _certificate_ref(data, label, required=False)
        ssl_policy = ssl_policy or DEFAULT_SSL_POLICY
        for index, item in enumerate(_list(data.get("sni_certificates"), f"{label}: sni_certificates")):
            where = f"{label}, SNI certificate {index + 1}"
            ref = _certificate_ref(schema.mapping(item, where, {"certificate", "certificate_arn"}), where, required=True)
            sni.append(ref)
    else:
        for key in ("ssl_policy", "certificate", "certificate_arn", "sni_certificates"):
            if data.get(key) is not None:
                raise ValueError(f"{label}: {key} only applies to {' or '.join(SECURE_PROTOCOLS)} listeners.")

    action_type = _choice(
        data.get("action"), f"{label}: action",
        ACTIONS if kind == "application" else ("forward",), default="forward",
    )
    inline = None
    action: ActionProps
    if action_type == "forward":
        _one_of(data, label, "target_group", "target_group_arn")
        target = data.get("target_group")
        if isinstance(target, Mapping):
            inline = TargetGroupProps.from_dict(target, label=f"{label}: target_group", default_name=name)
            target = inline.name
        elif target is not None:
            target = _lb_name(target, f"{label}: target_group")
        action = ActionProps("forward", (ForwardTargetProps(
            target_group=target,
            arn=_arn(data.get("target_group_arn"), f"{label}: target_group_arn", "target group"),
        ),))
    else:
        for key in ("target_group", "target_group_arn"):
            if data.get(key) is not None:
                raise ValueError(f"{label}: {key} only applies to forward actions.")
        action = _non_forward_action(action_type, data, label)
    return ListenerProps(
        name=name, kind=kind, port=port, protocol=protocol, action=action,
        load_balancer=_lb_name(data["load_balancer"], f"{label}: load_balancer") if data.get("load_balancer") is not None else None,
        load_balancer_arn=_arn(data.get("load_balancer_arn"), f"{label}: load_balancer_arn", "load balancer"),
        ssl_policy=ssl_policy, certificate=certificate, sni_certificates=tuple(sni),
        inline_target_group=inline,
    )


def _non_forward_action(action_type: str, data: Mapping[str, Any], label: str) -> ActionProps:
    if action_type == "fixed-response":
        if data.get("redirect") is not None:
            raise ValueError(f"{label}: redirect only applies to redirect actions.")
        return ActionProps("fixed-response", fixed_response=_fixed_response(data.get("fixed_response"), label))
    if data.get("fixed_response") is not None:
        raise ValueError(f"{label}: fixed_response only applies to fixed-response actions.")
    return ActionProps("redirect", redirect=_redirect(data.get("redirect"), label))


# --- listener rules --------------------------------------------------------


@dataclass(frozen=True)
class ConditionProps:
    field: str
    values: tuple[str, ...] = ()
    header_name: str | None = None
    query_strings: tuple[tuple[str | None, str], ...] = ()


@dataclass(frozen=True)
class ListenerRuleProps:
    name: str
    group: str
    priority: int
    action: ActionProps
    conditions: tuple[ConditionProps, ...]
    listener: str | None = None
    listener_arn: str | None = None


def _conditions(raw: Any, label: str) -> tuple[ConditionProps, ...]:
    result: list[ConditionProps] = []
    simple = {
        "host_headers": "host-header", "http_request_methods": "http-request-method",
        "path_patterns": "path-pattern", "source_ips": "source-ip",
    }
    for index, item in enumerate(_list(raw, f"{label}: conditions")):
        where = f"{label}, condition {index + 1}"
        data = schema.mapping(item, where, set(simple) | {"http_headers", "query_strings"})
        if not any(data.get(key) is not None for key in data):
            raise ValueError(f"{where}: give at least one match.")
        for key, field_name in simple.items():
            if data.get(key) is None:
                continue
            values = schema.strings(data[key], f"{where}: {key}", allow_empty=False)
            for value in values:
                if key == "http_request_methods" and not HTTP_METHOD.fullmatch(value):
                    raise ValueError(f"{where}: '{value}' is not an HTTP method.")
                if key == "source_ips":
                    try:
                        ipaddress.ip_network(value)
                    except ValueError:
                        raise ValueError(f"{where}: '{value}' is not a CIDR.") from None
            result.append(ConditionProps(field_name, values))
        for header_index, header in enumerate(_list(data.get("http_headers"), f"{where}: http_headers")):
            entry = schema.mapping(header, f"{where}, header {header_index + 1}", {"name", "values"}, required=("name", "values"))
            result.append(ConditionProps(
                "http-header",
                schema.strings(entry["values"], f"{where}: header values", allow_empty=False),
                header_name=schema.required_string(entry["name"], f"{where}: header name"),
            ))
        queries = []
        for query_index, query in enumerate(_list(data.get("query_strings"), f"{where}: query_strings")):
            entry = schema.mapping(query, f"{where}, query string {query_index + 1}", {"key", "value"}, required=("value",))
            queries.append((
                schema.string(entry.get("key"), f"{where}: query key"),
                schema.required_string(str(entry["value"]), f"{where}: query value"),
            ))
        if queries:
            result.append(ConditionProps("query-string", query_strings=tuple(queries)))
    if not result:
        raise ValueError(f"{label}: at least one condition is required.")
    return tuple(result)


def _rule_group(raw: Any, index: int) -> tuple[ListenerRuleProps, ...]:
    label = f"load_balancing.alb_listener_rules[{index}]"
    data = schema.mapping(raw, label, {"name", "listener", "listener_arn", "rules"}, required=("name", "rules"))
    group = schema.name(data["name"], f"{label} name")
    label = f"Listener rule group '{group}'"
    _one_of(data, label, "listener", "listener_arn")
    listener = schema.name(data["listener"], f"{label}: listener") if data.get("listener") is not None else None
    listener_arn = _arn(data.get("listener_arn"), f"{label}: listener_arn", "listener")
    rules = []
    for item in _list(data["rules"], f"{label}: rules"):
        entry = schema.mapping(item, f"{label} rule", {
            "name", "priority", "type", "target_groups", "fixed_response", "redirect", "conditions",
        }, required=("name", "priority", "type", "conditions"))
        name = schema.name(entry["name"], f"{label} rule name")
        where = f"Listener rule '{name}'"
        action_type = _choice(entry["type"], f"{where}: type", ACTIONS)
        if action_type == "forward":
            for key in ("fixed_response", "redirect"):
                if entry.get(key) is not None:
                    raise ValueError(f"{where}: {key} does not apply to forward rules.")
            forwards = []
            for target_index, target in enumerate(_list(entry.get("target_groups"), f"{where}: target_groups")):
                ref = schema.mapping(target, f"{where}, target group {target_index + 1}", {"target_group", "arn", "weight"})
                _one_of(ref, f"{where}, target group {target_index + 1}", "target_group", "arn")
                forwards.append(ForwardTargetProps(
                    target_group=_lb_name(ref["target_group"], f"{where}: target_group") if ref.get("target_group") is not None else None,
                    arn=_arn(ref.get("arn"), f"{where}: arn", "target group"),
                    weight=schema.integer(ref.get("weight"), f"{where}: weight", minimum=0, maximum=999),
                ))
            if not 1 <= len(forwards) <= MAX_FORWARD_TARGETS:
                raise ValueError(f"{where}: forward rules need 1-{MAX_FORWARD_TARGETS} target_groups.")
            action = ActionProps("forward", tuple(forwards))
        else:
            if entry.get("target_groups"):
                raise ValueError(f"{where}: target_groups only apply to forward rules.")
            action = _non_forward_action(action_type, entry, where)
        rules.append(ListenerRuleProps(
            name=name, group=group,
            priority=schema.integer(entry["priority"], f"{where}: priority", minimum=1, maximum=50000),
            action=action, conditions=_conditions(entry["conditions"], where),
            listener=listener, listener_arn=listener_arn,
        ))
    if not rules:
        raise ValueError(f"{label}: rules must not be empty.")
    return tuple(rules)


# --- load balancers --------------------------------------------------------


@dataclass(frozen=True)
class SubnetMappingProps:
    subnet: str
    private_ipv4_address: str | None = None


@dataclass(frozen=True)
class AccessLogsProps:
    bucket: str | None = None
    bucket_name: str | None = None
    prefix: str | None = None


@dataclass(frozen=True)
class LoadBalancerProps:
    name: str
    type: str
    internal: bool
    subnet_type: str
    security_groups: tuple[str, ...] = ()
    subnet_mappings: tuple[SubnetMappingProps, ...] = ()
    deletion_protection: bool = False
    drop_invalid_header_fields: bool = True
    access_logs: AccessLogsProps | None = None
    default_listener: ListenerProps | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def suffix(self) -> str:
        return "alb" if self.type == "application" else "nlb"

    @classmethod
    def from_dict(cls, raw: Any) -> LoadBalancerProps:
        data = schema.mapping(raw, "Load balancer", {
            "name", "type", "internal", "subnet_type", "security_groups", "subnet_mappings",
            "deletion_protection", "drop_invalid_header_fields", "access_logs",
            "default_listener", "tags",
        }, required=("name", "type"))
        name = _lb_name(data["name"], "Load balancer name")
        label = f"Load balancer '{name}'"
        lb_type = _choice(data["type"], f"{label}: type", LB_TYPES)
        if data.get("internal") is None:
            raise ValueError(
                f"{label}: internal is required (true for internal, false for internet-facing)."
            )
        internal = schema.boolean(data["internal"], f"{label}: internal")
        subnet_type = _choice(
            data.get("subnet_type"), f"{label}: subnet_type", ("public", "private"),
            default="private" if internal else "public",
        )
        if not internal and subnet_type == "private":
            raise ValueError(f"{label}: an internet-facing load balancer needs public subnets.")
        groups = schema.strings(data.get("security_groups"), f"{label}: security_groups")
        if len(set(groups)) != len(groups):
            raise ValueError(f"{label}: duplicate security group.")
        if lb_type == "application" and not groups:
            raise ValueError(
                f"{label}: application load balancers need at least one security group, so "
                "no unreviewed group is created."
            )
        mappings = []
        for index, item in enumerate(_list(data.get("subnet_mappings"), f"{label}: subnet_mappings")):
            where = f"{label}, subnet mapping {index + 1}"
            if lb_type != "network":
                raise ValueError(f"{label}: subnet_mappings only apply to network load balancers.")
            entry = schema.mapping(item, where, {"subnet", "private_ipv4_address"}, required=("subnet",))
            subnet = _choice(entry["subnet"], f"{where}: subnet", ORDINALS)
            address = schema.string(entry.get("private_ipv4_address"), f"{where}: private_ipv4_address")
            if address is not None:
                try:
                    ipaddress.IPv4Address(address)
                except ValueError:
                    raise ValueError(f"{where}: '{address}' is not an IPv4 address.") from None
                if not internal:
                    raise ValueError(f"{where}: private_ipv4_address needs an internal load balancer.")
            mappings.append(SubnetMappingProps(subnet, address))
        if len({m.subnet for m in mappings}) != len(mappings):
            raise ValueError(f"{label}: each subnet may be mapped once.")

        access_logs = None
        if data.get("access_logs") is not None:
            where = f"{label}: access_logs"
            entry = schema.mapping(data["access_logs"], where, {"bucket", "bucket_name", "prefix"})
            _one_of(entry, where, "bucket", "bucket_name")
            prefix = schema.string(entry.get("prefix"), f"{where} prefix")
            if prefix is not None and ("AWSLogs" in prefix or prefix.startswith("/") or prefix.endswith("/")):
                raise ValueError(f"{where}: prefix must not contain 'AWSLogs' or start/end with '/'.")
            access_logs = AccessLogsProps(
                bucket=schema.name(entry["bucket"], f"{where} bucket") if entry.get("bucket") is not None else None,
                bucket_name=schema.string(entry.get("bucket_name"), f"{where} bucket_name"),
                prefix=prefix,
            )

        default_listener = None
        if data.get("default_listener") is not None:
            if lb_type != "application":
                raise ValueError(f"{label}: default_listener only applies to application load balancers.")
            raw_listener = data["default_listener"]
            schema.mapping(raw_listener, f"{label}: default_listener", LISTENER_FIELDS | {"fixed_response", "redirect"})
            for key in ("load_balancer", "load_balancer_arn"):
                if key in raw_listener:
                    raise ValueError(f"{label}: default_listener always belongs to this load balancer; drop {key}.")
            default_listener = _listener(
                {"port": 443, "protocol": "HTTPS", "action": "fixed-response", **raw_listener, "load_balancer": name},
                "application", f"{label}: default_listener", default_name=f"{name}-default",
            )

        return cls(
            name=name, type=lb_type, internal=internal, subnet_type=subnet_type,
            security_groups=groups, subnet_mappings=tuple(mappings),
            deletion_protection=schema.boolean(data.get("deletion_protection"), f"{label}: deletion_protection"),
            drop_invalid_header_fields=schema.boolean(
                data.get("drop_invalid_header_fields"), f"{label}: drop_invalid_header_fields", default=True
            ),
            access_logs=access_logs, default_listener=default_listener,
            tags=schema.string_map(data.get("tags"), f"{label}: tags"),
        )


@dataclass(frozen=True)
class LoadBalancingProps:
    load_balancers: tuple[LoadBalancerProps, ...] = ()
    target_groups: tuple[TargetGroupProps, ...] = ()
    alb_listeners: tuple[ListenerProps, ...] = ()
    nlb_listeners: tuple[ListenerProps, ...] = ()
    listener_rules: tuple[ListenerRuleProps, ...] = ()

    def __bool__(self) -> bool:
        return any((
            self.load_balancers, self.target_groups, self.alb_listeners,
            self.nlb_listeners, self.listener_rules,
        ))

    @property
    def listeners(self) -> tuple[ListenerProps, ...]:
        defaults = tuple(lb.default_listener for lb in self.load_balancers if lb.default_listener)
        return defaults + self.alb_listeners + self.nlb_listeners

    def __post_init__(self) -> None:
        _unique(self.load_balancers, "load_balancing.load_balancers")
        _unique(self.target_groups, "load_balancing.target_groups (including inline listener target groups)")
        _unique(self.listeners, "load_balancing listeners (including default listeners)")
        _unique(self.listener_rules, "load_balancing.alb_listener_rules rules")
        lbs = {lb.name: lb for lb in self.load_balancers}
        tgs = {tg.name: tg for tg in self.target_groups}
        listeners = {listener.name: listener for listener in self.listeners}

        def target_family(target: ForwardTargetProps, family: str, label: str) -> None:
            if target.target_group is None:
                return
            group = schema.lookup(tgs, target.target_group, f"{label}: target_group")
            if group.family != family:
                raise ValueError(
                    f"{label}: target group '{group.name}' uses {group.protocol}, which an "
                    f"{family} load balancer cannot forward to."
                )

        for listener in self.listeners:
            label = f"Listener '{listener.name}'"
            if listener.load_balancer is not None:
                lb = schema.lookup(lbs, listener.load_balancer, f"{label}: load_balancer")
                if lb.type != listener.kind:
                    raise ValueError(f"{label}: load balancer '{lb.name}' is {lb.type}, not {listener.kind}.")
            for target in listener.action.targets:
                target_family(target, listener.kind, label)

        priorities: dict[tuple[str, int], str] = {}
        for rule in self.listener_rules:
            label = f"Listener rule '{rule.name}'"
            if rule.listener is not None:
                listener = schema.lookup(listeners, rule.listener, f"{label}: listener")
                if listener.kind != "application":
                    raise ValueError(f"{label}: listener rules only apply to ALB listeners.")
                key = (rule.listener, rule.priority)
                if key in priorities:
                    raise ValueError(
                        f"{label}: priority {rule.priority} is already used by '{priorities[key]}' "
                        f"on listener '{rule.listener}'."
                    )
                priorities[key] = rule.name
            for target in rule.action.targets:
                target_family(target, "application", label)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> LoadBalancingProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, "load_balancing", {
            "load_balancers", "target_groups", "alb_listeners", "alb_listener_rules", "nlb_listeners",
        })
        load_balancers = tuple(LoadBalancerProps.from_dict(item) for item in _list(data.get("load_balancers"), "load_balancing.load_balancers"))
        target_groups = tuple(TargetGroupProps.from_dict(item) for item in _list(data.get("target_groups"), "load_balancing.target_groups"))
        alb = tuple(
            _listener(item, "application", f"load_balancing.alb_listeners[{index}]")
            for index, item in enumerate(_list(data.get("alb_listeners"), "load_balancing.alb_listeners"))
        )
        nlb = tuple(
            _listener(item, "network", f"load_balancing.nlb_listeners[{index}]")
            for index, item in enumerate(_list(data.get("nlb_listeners"), "load_balancing.nlb_listeners"))
        )
        inline = tuple(
            listener.inline_target_group
            for listener in (*(lb.default_listener for lb in load_balancers if lb.default_listener), *alb, *nlb)
            if listener.inline_target_group is not None
        )
        parsed_groups = [
            _rule_group(group, index)
            for index, group in enumerate(_list(data.get("alb_listener_rules"), "load_balancing.alb_listener_rules"))
        ]
        group_names = [group[0].group.casefold() for group in parsed_groups]
        if len(set(group_names)) != len(group_names):
            raise ValueError("load_balancing.alb_listener_rules: duplicate group name.")
        rules = tuple(rule for group in parsed_groups for rule in group)
        return cls(
            load_balancers=load_balancers, target_groups=target_groups + inline,
            alb_listeners=alb, nlb_listeners=nlb, listener_rules=rules,
        )


# --- resources -------------------------------------------------------------


class LoadBalancing(Construct):
    """Builds load balancers, then target groups, listeners and rules."""

    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        common: Common,
        network_name: str,
        account_id: str,
        props: LoadBalancingProps,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        certificates: Mapping[str, acm.ICertificate] | None = None,
        buckets: Mapping[str, s3.IBucket] | None = None,
    ) -> None:
        super().__init__(scope, construct_id_)
        self.common = common
        self.network_name = network_name
        self.account_id = account_id
        self.network = network
        self.security_groups = security_groups
        self.certificates = certificates or {}
        self.buckets = buckets or {}
        self.load_balancers: dict[str, elbv2.BaseLoadBalancer] = {}
        self.target_groups: dict[str, elbv2.TargetGroupBase] = {}
        self.listeners: dict[str, elbv2.CfnListener] = {}
        self.listener_rules: dict[str, elbv2.CfnListenerRule] = {}

        for lb in props.load_balancers:
            self.load_balancers[lb.name] = self._load_balancer(lb)
        for group in props.target_groups:
            self.target_groups[group.name] = self._target_group(group)
        for listener in props.listeners:
            self.listeners[listener.name] = self._listener(listener)
        for rule in props.listener_rules:
            self.listener_rules[rule.name] = self._rule(rule)

    def physical_name(self, name: str, suffix: str, label: str) -> str:
        value = self.common.abbreviated_name(self.network_name, name, suffix)
        if len(value) > MAX_NAME:
            raise ValueError(
                f"{label} '{name}': AWS name '{value}' has {len(value)} characters; the "
                f"limit is {MAX_NAME}. Use a shorter name."
            )
        return value

    # --- load balancers ---------------------------------------------------

    def _load_balancer(self, props: LoadBalancerProps) -> elbv2.BaseLoadBalancer:
        label = f"Load balancer '{props.name}'"
        name = self.physical_name(props.name, props.suffix, "Load balancer")
        subnets = self.network.public_subnets if props.subnet_type == "public" else self.network.private_subnets
        if not subnets:
            raise ValueError(f"{label}: the network has no {props.subnet_type} subnets.")
        groups = [
            schema.lookup(self.security_groups, key, f"{label}: security group")
            for key in props.security_groups
        ]
        common_props = dict(
            vpc=self.network.vpc,
            internet_facing=not props.internal,
            vpc_subnets=self.network.subnet_selection(props.subnet_type),
            load_balancer_name=name,
            deletion_protection=props.deletion_protection,
        )
        cid = f"{construct_id(props.name)}LoadBalancer"
        lb: elbv2.BaseLoadBalancer
        if props.type == "application":
            lb = elbv2.ApplicationLoadBalancer(
                self, cid, security_group=groups[0],
                drop_invalid_header_fields=props.drop_invalid_header_fields, **common_props,
            )
            for group in groups[1:]:
                lb.add_security_group(group)
        else:
            lb = elbv2.NetworkLoadBalancer(self, cid, security_groups=groups or None, **common_props)

        if props.subnet_mappings:
            self._subnet_mappings(lb, props, subnets, label)
        if props.access_logs is not None:
            self._access_logs(lb, props, label)
        cdk.Tags.of(lb).add("Name", name)
        for key, value in props.tags.items():
            cdk.Tags.of(lb).add(key, value)
        return lb

    def _subnet_mappings(
        self, lb: elbv2.BaseLoadBalancer, props: LoadBalancerProps,
        subnets: list[ec2.ISubnet], label: str,
    ) -> None:
        public, private = self.network.props.subnet_cidrs()
        cidrs = public if props.subnet_type == "public" else private
        mappings = []
        for mapping in props.subnet_mappings:
            index = ORDINALS.index(mapping.subnet)
            if index >= len(subnets):
                raise ValueError(
                    f"{label}: there is no {mapping.subnet} {props.subnet_type} subnet "
                    f"(the network has {len(subnets)})."
                )
            entry: dict[str, str] = {"SubnetId": subnets[index].subnet_id}
            if mapping.private_ipv4_address is not None:
                block = ipaddress.ip_network(cidrs[index])
                address = ipaddress.ip_address(mapping.private_ipv4_address)
                reserved = {block.network_address + offset for offset in range(4)} | {block.broadcast_address}
                if address not in block or address in reserved:
                    raise ValueError(
                        f"{label}: {address} is not a usable address in the {mapping.subnet} "
                        f"{props.subnet_type} subnet {block}."
                    )
                entry["PrivateIPv4Address"] = str(address)
            mappings.append(entry)
        cfn = lb.node.default_child
        cfn.add_property_deletion_override("Subnets")
        cfn.add_property_override("SubnetMappings", mappings)

    def _access_logs(self, lb: elbv2.BaseLoadBalancer, props: LoadBalancerProps, label: str) -> None:
        logs = props.access_logs
        bucket = (
            schema.lookup(self.buckets, logs.bucket, f"{label}: access_logs bucket").bucket_name
            if logs.bucket is not None else logs.bucket_name
        )
        # Attributes only: the bucket policy belongs to the stack that owns the
        # bucket, so this stack never edits another stack's resources.
        lb.set_attribute("access_logs.s3.enabled", "true")
        lb.set_attribute("access_logs.s3.bucket", bucket)
        lb.set_attribute(
            "access_logs.s3.prefix",
            logs.prefix or f"{self.account_id}/elb-logs/{props.type}/{props.name}",
        )

    # --- target groups ----------------------------------------------------

    def _target_group(self, props: TargetGroupProps) -> elbv2.TargetGroupBase:
        name = self.physical_name(props.name, "tg", "Target group")
        target_type = elbv2.TargetType.INSTANCE if props.target_type == "instance" else elbv2.TargetType.IP
        members = [
            targets.InstanceIdTarget(item.target_id, item.port)
            if props.target_type == "instance" else targets.IpTarget(item.target_id, item.port)
            for item in props.attachments
        ]
        cid = f"{construct_id(props.name)}TargetGroup"
        common_props = dict(
            vpc=self.network.vpc, target_group_name=name, port=props.port,
            target_type=target_type, health_check=self._health_check(props),
            targets=members or None,
        )
        group: elbv2.TargetGroupBase
        if props.family == "application":
            group = elbv2.ApplicationTargetGroup(
                self, cid, protocol=getattr(elbv2.ApplicationProtocol, props.protocol), **common_props,
            )
        else:
            group = elbv2.NetworkTargetGroup(
                self, cid, protocol=getattr(elbv2.Protocol, props.protocol),
                preserve_client_ip=props.preserve_client_ip, **common_props,
            )
        if props.stickiness is not None:
            sticky = props.stickiness
            group.set_attribute("stickiness.enabled", str(sticky.enabled).lower())
            group.set_attribute("stickiness.type", sticky.type)
            if sticky.type == "lb_cookie" and sticky.cookie_duration is not None:
                group.set_attribute("stickiness.lb_cookie.duration_seconds", str(sticky.cookie_duration))
            if sticky.type == "app_cookie":
                group.set_attribute("stickiness.app_cookie.cookie_name", sticky.cookie_name)
                if sticky.cookie_duration is not None:
                    group.set_attribute("stickiness.app_cookie.duration_seconds", str(sticky.cookie_duration))
        cdk.Tags.of(group).add("Name", name)
        for key, value in props.tags.items():
            cdk.Tags.of(group).add(key, value)
        return group

    @staticmethod
    def _health_check(props: TargetGroupProps) -> elbv2.HealthCheck | None:
        check = props.health_check
        if check is None:
            return None
        return elbv2.HealthCheck(
            enabled=True,
            protocol=getattr(elbv2.Protocol, check.protocol) if check.protocol else None,
            # Terraform leaves the port at traffic-port unless it differs.
            port=str(check.port) if check.port is not None and check.port != props.port else None,
            path=check.path,
            healthy_http_codes=check.matcher,
            interval=cdk.Duration.seconds(check.interval),
            timeout=cdk.Duration.seconds(check.timeout),
            healthy_threshold_count=check.healthy_threshold,
            unhealthy_threshold_count=check.unhealthy_threshold,
        )

    # --- listeners --------------------------------------------------------

    def _certificate_arn(self, ref: CertificateRef | None, label: str) -> str:
        if ref is not None and ref.arn is not None:
            return ref.arn
        if ref is not None and ref.certificate is not None:
            return schema.lookup(self.certificates, ref.certificate, f"{label}: certificate").certificate_arn
        if self.network_name in self.certificates:
            return self.certificates[self.network_name].certificate_arn
        raise ValueError(
            f"{label}: a secure listener needs certificate or certificate_arn (or a "
            f"certificate named '{self.network_name}' after the network)."
        )

    def _target_group_arn(self, target: ForwardTargetProps) -> str:
        if target.arn is not None:
            return target.arn
        return self.target_groups[target.target_group].target_group_arn

    def _action(self, cls: Any, action: ActionProps) -> Any:
        if action.type == "forward":
            if len(action.targets) == 1:
                return cls.ActionProperty(type="forward", target_group_arn=self._target_group_arn(action.targets[0]))
            return cls.ActionProperty(
                type="forward",
                forward_config=cls.ForwardConfigProperty(target_groups=[
                    cls.TargetGroupTupleProperty(
                        target_group_arn=self._target_group_arn(target), weight=target.weight,
                    )
                    for target in action.targets
                ]),
            )
        if action.type == "fixed-response":
            fixed = action.fixed_response
            return cls.ActionProperty(
                type="fixed-response",
                fixed_response_config=cls.FixedResponseConfigProperty(
                    status_code=fixed.status_code, content_type=fixed.content_type,
                    message_body=fixed.message_body,
                ),
            )
        redirect = action.redirect
        return cls.ActionProperty(
            type="redirect",
            redirect_config=cls.RedirectConfigProperty(
                status_code=redirect.status_code, protocol=redirect.protocol, port=redirect.port,
                host=redirect.host, path=redirect.path, query=redirect.query,
            ),
        )

    def _listener(self, props: ListenerProps) -> elbv2.CfnListener:
        label = f"Listener '{props.name}'"
        lb_arn = (
            self.load_balancers[props.load_balancer].load_balancer_arn
            if props.load_balancer is not None else props.load_balancer_arn
        )
        cid = construct_id(props.name)
        listener = elbv2.CfnListener(
            self, f"{cid}Listener",
            load_balancer_arn=lb_arn,
            port=props.port,
            protocol=props.protocol,
            ssl_policy=props.ssl_policy if props.secure else None,
            certificates=[elbv2.CfnListener.CertificateProperty(
                certificate_arn=self._certificate_arn(props.certificate, label)
            )] if props.secure else None,
            default_actions=[self._action(elbv2.CfnListener, props.action)],
        )
        cdk.Tags.of(listener).add(
            "Name", self.common.abbreviated_name(self.network_name, props.name, "listener")
        )
        if props.sni_certificates:
            elbv2.CfnListenerCertificate(
                self, f"{cid}SniCertificates",
                listener_arn=listener.attr_listener_arn,
                certificates=[
                    elbv2.CfnListenerCertificate.CertificateProperty(
                        certificate_arn=self._certificate_arn(ref, label)
                    )
                    for ref in props.sni_certificates
                ],
            )
        return listener

    def _rule(self, props: ListenerRuleProps) -> elbv2.CfnListenerRule:
        cls = elbv2.CfnListenerRule
        conditions = []
        for condition in props.conditions:
            values = list(condition.values)
            config: dict[str, Any] = {}
            if condition.field == "host-header":
                config["host_header_config"] = cls.HostHeaderConfigProperty(values=values)
            elif condition.field == "http-request-method":
                config["http_request_method_config"] = cls.HttpRequestMethodConfigProperty(values=values)
            elif condition.field == "path-pattern":
                config["path_pattern_config"] = cls.PathPatternConfigProperty(values=values)
            elif condition.field == "source-ip":
                config["source_ip_config"] = cls.SourceIpConfigProperty(values=values)
            elif condition.field == "http-header":
                config["http_header_config"] = cls.HttpHeaderConfigProperty(
                    http_header_name=condition.header_name, values=values,
                )
            else:
                config["query_string_config"] = cls.QueryStringConfigProperty(values=[
                    cls.QueryStringKeyValueProperty(key=key, value=value)
                    for key, value in condition.query_strings
                ])
            conditions.append(cls.RuleConditionProperty(field=condition.field, **config))
        listener_arn = (
            self.listeners[props.listener].attr_listener_arn
            if props.listener is not None else props.listener_arn
        )
        return cls(
            self, f"{construct_id(props.name)}ListenerRule",
            listener_arn=listener_arn,
            priority=props.priority,
            actions=[self._action(cls, props.action)],
            conditions=conditions,
        )
