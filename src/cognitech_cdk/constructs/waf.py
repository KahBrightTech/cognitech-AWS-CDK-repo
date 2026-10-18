"""WAFv2 IP sets, rule groups and web ACLs, mirroring the Terraform `Waf-ipsets`,
`WAF-rulegroup` and `WAF` modules.

    waf:
      ip_sets:
        - name: office
          addresses: [203.0.113.0/24]
      rule_groups:
        - name: edge
          capacity: 100
          rules:
            - {name: block-office, priority: 1, action: block, statement_type: ip_set, ip_set: office}
      web_acls:
        - name: app
          default_action: allow
          rule_file: documents/waf/managed-rules.json   # {"rules": [...]} or a list
          rules:
            - {name: geo, priority: 20, action: block, statement_type: geo_match, country_codes: [KP]}
            - {name: edge, priority: 30, statement_type: rule_group_reference, rule_group: edge}
          association: {load_balancers: [app]}
          logging: {enabled: true, log_retention_days: 30}

Managed rule groups and rule-group references use `override_action`
(`none`, the default, or `count`); WAF rejects a plain action on them.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from cognitech_cdk.common import schema
from cognitech_cdk.common.documents import load_document

ACTIONS = ("allow", "block", "count")
OVERRIDES = ("none", "count")
STATEMENTS = (
    "ip_set", "geo_match", "rate_limit", "rate_based", "byte_match", "sqli_match",
    "xss_match", "size_constraint", "managed_rule_group", "rule_group_reference",
)
GROUP_ONLY = ("managed_rule_group", "rule_group_reference")
FIELDS = ("uri_path", "query_string", "body", "single_header")
POSITIONS = ("EXACTLY", "STARTS_WITH", "ENDS_WITH", "CONTAINS", "CONTAINS_WORD")
TRANSFORMATIONS = ("NONE", "COMPRESS_WHITE_SPACE", "HTML_ENTITY_DECODE", "LOWERCASE",
                   "CMD_LINE", "URL_DECODE", "BASE64_DECODE", "HEX_DECODE", "MD5", "REPLACE_COMMENTS",
                   "ESCAPE_SEQ_DECODE", "SQL_HEX_DECODE", "CSS_DECODE", "JS_DECODE",
                   "NORMALIZE_PATH", "NORMALIZE_PATH_WIN", "REMOVE_NULLS", "REPLACE_NULLS",
                   "BASE64_DECODE_EXT", "URL_DECODE_UNI", "UTF8_TO_UNICODE")
OPERATORS = ("EQ", "NE", "LE", "LT", "GE", "GT")
AGGREGATE = ("IP", "FORWARDED_IP", "CONSTANT")
SCOPES = ("REGIONAL", "CLOUDFRONT")
ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{0,12}:.+")
COUNTRY = re.compile(r"[A-Z]{2}")
METRIC = re.compile(r"[\w#:.-]{1,128}")
LOG_RETENTION_DAYS = (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827,
                      2192, 2557, 2922, 3288, 3653)


def _choice(value: Any, label: str, choices: tuple[str, ...], default: str | None = None) -> str | None:
    result = schema.string(value, label, default=default)
    if result is not None and result not in choices:
        raise ValueError(f"{label} must be one of {', '.join(choices)}.")
    return result


def _arn(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not ARN.fullmatch(value):
        raise ValueError(f"{label} must be an ARN.")
    return value


@dataclass(frozen=True)
class RuleProps:
    name: str
    priority: int
    statement_type: str
    action: str | None = None
    override_action: str | None = None
    ip_set: str | None = None
    ip_set_arn: str | None = None
    country_codes: tuple[str, ...] = ()
    rate_limit: int | None = None
    aggregate_key_type: str = "IP"
    field_to_match: str | None = None
    header_name: str | None = None
    positional_constraint: str | None = None
    search_string: str | None = None
    text_transformation: str = "NONE"
    comparison_operator: str | None = None
    size: int | None = None
    vendor_name: str = "AWS"
    managed_rule_group_name: str | None = None
    rule_overrides: tuple[tuple[str, str], ...] = ()
    scope_down_country_codes: tuple[str, ...] = ()
    rule_group: str | None = None
    rule_group_arn: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, owner: str, *, in_rule_group: bool = False) -> RuleProps:
        data = schema.mapping(raw, f"{owner} rule", {
            "name", "priority", "action", "override_action", "statement_type", "ip_set", "ip_set_key",
            "ip_set_arn", "country_codes", "rate_limit", "aggregate_key_type", "field_to_match",
            "header_name", "positional_constraint", "search_string", "text_transformation",
            "comparison_operator", "size", "vendor_name", "managed_rule_group_name", "rule_overrides",
            "scope_downs", "rule_group", "rule_group_key", "arn", "rule_group_arn",
        }, required=("name", "priority", "statement_type"))
        name = schema.name(data["name"], f"{owner} rule name")
        label = f"{owner}, rule '{name}'"
        kind = _choice(data["statement_type"], f"{label}: statement_type", STATEMENTS)
        if kind == "rate_based":
            kind = "rate_limit"
        if in_rule_group and kind in GROUP_ONLY:
            raise ValueError(f"{label}: rule groups cannot contain {kind} statements.")
        grouped = kind in GROUP_ONLY
        action = override = None
        if grouped:
            raw_override = data.get("override_action")
            # Terraform rule files set `action` on managed groups; map it to the only valid override.
            if raw_override is None and data.get("action") is not None:
                raw_override = "count" if data["action"] == "count" else "none"
            override = _choice(raw_override, f"{label}: override_action", OVERRIDES, "none")
        else:
            if data.get("override_action") is not None:
                raise ValueError(f"{label}: override_action only applies to rule groups.")
            action = _choice(data.get("action"), f"{label}: action", ACTIONS)
            if action is None:
                raise ValueError(f"{label}: action is required.")
        ip_set = data.get("ip_set", data.get("ip_set_key"))
        ip_set_arn = _arn(data.get("ip_set_arn"), f"{label}: ip_set_arn")
        if kind == "ip_set" and (ip_set is None) == (ip_set_arn is None):
            raise ValueError(f"{label}: provide exactly one of ip_set or ip_set_arn.")
        codes = schema.strings(data.get("country_codes"), f"{label}: country_codes")
        if kind == "geo_match" and not codes:
            raise ValueError(f"{label}: country_codes is required for geo_match.")
        for code in codes:
            if not COUNTRY.fullmatch(code):
                raise ValueError(f"{label}: '{code}' is not a two-letter country code.")
        rate = schema.integer(data.get("rate_limit"), f"{label}: rate_limit", minimum=10, maximum=2_000_000_000)
        if kind == "rate_limit" and rate is None:
            raise ValueError(f"{label}: rate_limit is required.")
        field_name = _choice(data.get("field_to_match"), f"{label}: field_to_match", FIELDS)
        if kind in ("byte_match", "sqli_match", "xss_match", "size_constraint") and field_name is None:
            raise ValueError(f"{label}: field_to_match is required for {kind}.")
        header = schema.string(data.get("header_name"), f"{label}: header_name")
        if (field_name == "single_header") != (header is not None):
            raise ValueError(f"{label}: header_name is required with, and only with, single_header.")
        position = _choice(data.get("positional_constraint"), f"{label}: positional_constraint", POSITIONS)
        search = schema.string(data.get("search_string"), f"{label}: search_string")
        if kind == "byte_match" and (position is None or search is None):
            raise ValueError(f"{label}: byte_match needs positional_constraint and search_string.")
        operator = _choice(data.get("comparison_operator"), f"{label}: comparison_operator", OPERATORS)
        size = schema.integer(data.get("size"), f"{label}: size", minimum=0)
        if kind == "size_constraint" and (operator is None or size is None):
            raise ValueError(f"{label}: size_constraint needs comparison_operator and size.")
        managed = schema.string(data.get("managed_rule_group_name"), f"{label}: managed_rule_group_name")
        if kind == "managed_rule_group" and managed is None:
            managed = name
        overrides = []
        for entry in data.get("rule_overrides") or []:
            item = schema.mapping(entry, f"{label}: rule_overrides", {"name", "action"}, required=("name", "action"))
            overrides.append((
                schema.required_string(item["name"], f"{label}: rule_overrides name"),
                _choice(item["action"], f"{label}: rule_overrides action", ACTIONS) or "",
            ))
        scope_codes: tuple[str, ...] = ()
        if data.get("scope_downs") is not None:
            scope = schema.mapping(data["scope_downs"], f"{label}: scope_downs", {"geo_match"})
            geo = schema.mapping(scope.get("geo_match") or {}, f"{label}: scope_downs.geo_match",
                                 {"country_codes"}, required=("country_codes",))
            scope_codes = schema.strings(geo["country_codes"], f"{label}: scope_downs country_codes")
        group = data.get("rule_group", data.get("rule_group_key"))
        group_arn = _arn(data.get("rule_group_arn", data.get("arn")), f"{label}: rule_group_arn")
        if kind == "rule_group_reference" and (group is None) == (group_arn is None):
            raise ValueError(f"{label}: provide exactly one of rule_group or rule_group_arn.")
        return cls(
            name=name,
            priority=schema.integer(data["priority"], f"{label}: priority", minimum=0) or 0,
            statement_type=kind or "",
            action=action,
            override_action=override,
            ip_set=schema.name(ip_set, f"{label}: ip_set") if ip_set is not None else None,
            ip_set_arn=ip_set_arn,
            country_codes=codes,
            rate_limit=rate,
            aggregate_key_type=_choice(data.get("aggregate_key_type"), f"{label}: aggregate_key_type",
                                       AGGREGATE, "IP") or "IP",
            field_to_match=field_name,
            header_name=header,
            positional_constraint=position,
            search_string=search,
            text_transformation=_choice(data.get("text_transformation"), f"{label}: text_transformation",
                                        TRANSFORMATIONS, "NONE") or "NONE",
            comparison_operator=operator,
            size=size,
            vendor_name=schema.string(data.get("vendor_name"), f"{label}: vendor_name", default="AWS") or "AWS",
            managed_rule_group_name=managed,
            rule_overrides=tuple(overrides),
            scope_down_country_codes=scope_codes,
            rule_group=schema.name(group, f"{label}: rule_group") if group is not None else None,
            rule_group_arn=group_arn,
        )


def _rules(raw: Any, rule_file: Any, owner: str, *, base_dir: Path | None, in_rule_group: bool) -> tuple[RuleProps, ...]:
    entries = list(raw or [])
    if not isinstance(raw or [], list):
        raise ValueError(f"{owner}: rules must be a list.")
    if rule_file is not None:
        if not isinstance(rule_file, str):
            raise ValueError(f"{owner}: rule_file must be a repository-relative path.")
        document = load_document(rule_file, f"{owner}: rule_file", base_dir=base_dir)
        if isinstance(document, Mapping):
            document = document.get("rules")
        if not isinstance(document, list):
            raise ValueError(f"{owner}: rule_file must contain a list of rules or a 'rules' list.")
        entries = document + entries
    rules = tuple(RuleProps.from_dict(entry, owner, in_rule_group=in_rule_group) for entry in entries)
    for kind, values in (("name", [r.name for r in rules]), ("priority", [r.priority for r in rules])):
        if len(values) != len(set(values)):
            raise ValueError(f"{owner}: duplicate rule {kind}.")
    return rules


@dataclass(frozen=True)
class IpSetProps:
    name: str
    addresses: tuple[str, ...]
    ip_address_version: str = "IPV4"
    description: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> IpSetProps:
        data = schema.mapping(raw, "WAF IP set", {
            "name", "key", "addresses", "ip_address_version", "description", "additional_tags", "scope",
        }, required=("name", "addresses"))
        name = schema.name(data["name"], "WAF IP set name")
        label = f"WAF IP set '{name}'"
        version = _choice(data.get("ip_address_version"), f"{label}: ip_address_version",
                          ("IPV4", "IPV6"), "IPV4") or "IPV4"
        addresses = schema.strings(data["addresses"], f"{label}: addresses")
        for address in addresses:
            try:
                network = ipaddress.ip_network(address, strict=True)
            except ValueError as error:
                raise ValueError(f"{label}: '{address}' is not a CIDR: {error}") from error
            if f"IPV{network.version}" != version or "/" not in address:
                raise ValueError(f"{label}: '{address}' must be an {version} CIDR such as 203.0.113.0/24.")
        return cls(
            name=name,
            addresses=addresses,
            ip_address_version=version,
            description=schema.string(data.get("description"), f"{label}: description"),
            tags=schema.string_map(data.get("additional_tags"), f"{label}: additional_tags"),
        )


@dataclass(frozen=True)
class RuleGroupProps:
    name: str
    capacity: int
    rules: tuple[RuleProps, ...]
    description: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None) -> RuleGroupProps:
        data = schema.mapping(raw, "WAF rule group", {
            "name", "key", "capacity", "rules", "rule_group_file", "rule_file", "description", "additional_tags",
        }, required=("name", "capacity"))
        name = schema.name(data["name"], "WAF rule group name")
        label = f"WAF rule group '{name}'"
        rules = _rules(data.get("rules"), data.get("rule_file", data.get("rule_group_file")), label,
                       base_dir=base_dir, in_rule_group=True)
        if not rules:
            raise ValueError(f"{label}: at least one rule is required.")
        return cls(
            name=name,
            capacity=schema.integer(data["capacity"], f"{label}: capacity", minimum=1, maximum=5000) or 1,
            rules=rules,
            description=schema.string(data.get("description"), f"{label}: description"),
            tags=schema.string_map(data.get("additional_tags"), f"{label}: additional_tags"),
        )


@dataclass(frozen=True)
class WebAclProps:
    name: str
    rules: tuple[RuleProps, ...] = ()
    description: str = "WAF Web ACL for application protection"
    scope: str = "REGIONAL"
    default_action: str = "allow"
    metrics_enabled: bool = True
    sampled_requests_enabled: bool = True
    load_balancers: tuple[str, ...] = ()
    load_balancer_arns: tuple[str, ...] = ()
    logging_enabled: bool = False
    log_retention_days: int = 30
    redacted_fields: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None) -> WebAclProps:
        data = schema.mapping(raw, "WAF web ACL", {
            "name", "key", "description", "scope", "default_action", "cloudwatch_metrics_enabled",
            "sampled_requests_enabled", "rule_file", "rules", "custom_rules", "rule_group_references",
            "association", "logging",
        }, required=("name",))
        name = schema.name(data["name"], "WAF web ACL name")
        label = f"WAF web ACL '{name}'"
        extra = list(data.get("rules") or []) + list(data.get("custom_rules") or [])
        extra += [
            {**reference, "statement_type": "rule_group_reference"}
            for reference in data.get("rule_group_references") or []
        ]
        rules = _rules(extra, data.get("rule_file"), label, base_dir=base_dir, in_rule_group=False)
        scope = _choice(data.get("scope"), f"{label}: scope", SCOPES, "REGIONAL") or "REGIONAL"
        association = schema.mapping(data.get("association") or {}, f"{label}: association", {
            "load_balancers", "alb_keys", "load_balancer_arns", "alb_arns", "associate_alb",
        })
        balancers = schema.strings(
            association.get("load_balancers", association.get("alb_keys")), f"{label}: association.load_balancers"
        )
        arns = tuple(
            _arn(arn, f"{label}: association.load_balancer_arns") or ""
            for arn in schema.strings(
                association.get("load_balancer_arns", association.get("alb_arns")),
                f"{label}: association.load_balancer_arns",
            )
        )
        if (balancers or arns) and scope != "REGIONAL":
            raise ValueError(f"{label}: only REGIONAL web ACLs can be associated with load balancers.")
        logging = schema.mapping(data.get("logging") or {}, f"{label}: logging", {
            "enabled", "create_log_group", "log_retention_days", "redacted_fields",
        })
        days = schema.integer(logging.get("log_retention_days"), f"{label}: logging.log_retention_days",
                              default=30, minimum=1) or 30
        if days not in LOG_RETENTION_DAYS:
            raise ValueError(f"{label}: logging.log_retention_days must be a CloudWatch retention value.")
        redacted = schema.strings(logging.get("redacted_fields"), f"{label}: logging.redacted_fields")
        for item in redacted:
            if item not in ("uri_path", "query_string", "method") and not item.startswith("single_header:"):
                raise ValueError(f"{label}: redacted field '{item}' must be uri_path, query_string, "
                                 "method or single_header:<name>.")
        return cls(
            name=name,
            rules=rules,
            description=schema.string(data.get("description"), f"{label}: description",
                                      default="WAF Web ACL for application protection") or "",
            scope=scope,
            default_action=_choice(data.get("default_action"), f"{label}: default_action",
                                   ("allow", "block"), "allow") or "allow",
            metrics_enabled=schema.boolean(data.get("cloudwatch_metrics_enabled"),
                                           f"{label}: cloudwatch_metrics_enabled", default=True),
            sampled_requests_enabled=schema.boolean(data.get("sampled_requests_enabled"),
                                                    f"{label}: sampled_requests_enabled", default=True),
            load_balancers=balancers,
            load_balancer_arns=arns,
            logging_enabled=schema.boolean(logging.get("enabled"), f"{label}: logging.enabled"),
            log_retention_days=days,
            redacted_fields=redacted,
        )


@dataclass(frozen=True)
class WafProps:
    ip_sets: tuple[IpSetProps, ...] = ()
    rule_groups: tuple[RuleGroupProps, ...] = ()
    web_acls: tuple[WebAclProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.ip_sets or self.rule_groups or self.web_acls)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> WafProps:
        if raw is None:
            return cls()
        data = schema.mapping(raw, "waf", {"ip_sets", "rule_groups", "web_acls"})
        props = cls(
            ip_sets=schema.items(data.get("ip_sets"), "waf.ip_sets", IpSetProps.from_dict),
            rule_groups=schema.items(
                data.get("rule_groups"), "waf.rule_groups",
                lambda item: RuleGroupProps.from_dict(item, base_dir=base_dir),
            ),
            web_acls=schema.items(
                data.get("web_acls"), "waf.web_acls",
                lambda item: WebAclProps.from_dict(item, base_dir=base_dir),
            ),
        )
        ip_sets = {item.name for item in props.ip_sets}
        groups = {item.name for item in props.rule_groups}
        for owner, rules in (
            *((f"WAF rule group '{g.name}'", g.rules) for g in props.rule_groups),
            *((f"WAF web ACL '{w.name}'", w.rules) for w in props.web_acls),
        ):
            for rule in rules:
                if rule.ip_set is not None and rule.ip_set not in ip_sets:
                    raise ValueError(f"{owner}, rule '{rule.name}': unknown ip_set '{rule.ip_set}'.")
                if rule.rule_group is not None and rule.rule_group not in groups:
                    raise ValueError(f"{owner}, rule '{rule.name}': unknown rule_group '{rule.rule_group}'.")
        return props


def statement(rule: RuleProps, *, ip_set_arn: str | None, rule_group_arn: str | None) -> dict[str, Any]:
    """CloudFormation (PascalCase) WAFv2 statement for one rule."""
    def match_field() -> dict[str, Any]:
        return {
            "uri_path": {"UriPath": {}},
            "query_string": {"QueryString": {}},
            "body": {"Body": {}},
            "single_header": {"SingleHeader": {"Name": rule.header_name}},
        }[rule.field_to_match or ""]

    transformations = [{"Priority": 0, "Type": rule.text_transformation}]
    kind = rule.statement_type
    if kind == "ip_set":
        return {"IPSetReferenceStatement": {"Arn": ip_set_arn}}
    if kind == "geo_match":
        return {"GeoMatchStatement": {"CountryCodes": list(rule.country_codes)}}
    if kind == "rate_limit":
        return {"RateBasedStatement": {"Limit": rule.rate_limit, "AggregateKeyType": rule.aggregate_key_type}}
    if kind == "byte_match":
        return {"ByteMatchStatement": {
            "FieldToMatch": match_field(), "PositionalConstraint": rule.positional_constraint,
            "SearchString": rule.search_string, "TextTransformations": transformations,
        }}
    if kind in ("sqli_match", "xss_match"):
        key = "SqliMatchStatement" if kind == "sqli_match" else "XssMatchStatement"
        return {key: {"FieldToMatch": match_field(), "TextTransformations": transformations}}
    if kind == "size_constraint":
        return {"SizeConstraintStatement": {
            "FieldToMatch": match_field(), "ComparisonOperator": rule.comparison_operator,
            "Size": rule.size, "TextTransformations": transformations,
        }}
    if kind == "managed_rule_group":
        managed: dict[str, Any] = {"VendorName": rule.vendor_name, "Name": rule.managed_rule_group_name}
        if rule.rule_overrides:
            managed["RuleActionOverrides"] = [
                {"Name": name, "ActionToUse": {action.capitalize(): {}}}
                for name, action in rule.rule_overrides
            ]
        if rule.scope_down_country_codes:
            managed["ScopeDownStatement"] = {
                "GeoMatchStatement": {"CountryCodes": list(rule.scope_down_country_codes)}
            }
        return {"ManagedRuleGroupStatement": managed}
    return {"RuleGroupReferenceStatement": {"Arn": rule_group_arn}}
