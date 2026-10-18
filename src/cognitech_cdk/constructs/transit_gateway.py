"""Transit gateway, its VPC attachments, route tables and the routes into it.

Ports the Terraform Tenant-account transit gateway modules. Every Terraform
variable becomes one section of the single `transit_gateway:` mapping:

    Terraform variable    YAML section
    transit_gateway       gateway             (or transit_gateway_id for an existing one)
    tgw_attachments       attachments
    tgw_route_table       route_tables
    tgw_associations      associations
    tgw_propagations      propagations
    tgw_routes            routes
    tgw_subnet_route      subnet_routes

Example:

    transit_gateway:
      gateway:
        name: shared
        amazon_side_asn: 64512
        default_route_table_association: disable
        default_route_table_propagation: disable
      attachments:
        - name: uat                    # one subnet per AZ from the private tier
      route_tables:
        - key: shared
          name: shared
      associations:
        - key: uat-shared
          attachment: uat              # or attachment_id: tgw-attach-...
          route_table: shared          # or route_table_id: tgw-rtb-...
      propagations:
        - key: uat-shared
          attachment: uat
          route_table: shared
      routes:
        - key: on-prem
          destination_cidr_block: 10.0.0.0/8
          attachment: uat
          route_table: shared
      subnet_routes:
        - name: corporate
          cidr_block: 10.0.0.0/8
          subnet_type: private
          availability_zones: [primary, secondary]   # default: every AZ

When a reference to an attachment or route table is left out and exactly one
is configured, that one is used. Resource names follow the repo convention:

    {account_name}-{region_prefix}-{vpc_name}-{name}-tgw
    {account_name}-{region_prefix}-{vpc_name}-{name}-tgw-attach
    {account_name}-{region_prefix}-{vpc_name}-{name}-tgw-rtb
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ram as ram
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.config import MAX_AZS, ORDINALS, Common, ordinal
from cognitech_cdk.constructs.network import Network

SUBNET_TYPES = ("public", "private")
TOGGLES = ("enable", "disable")
DEFAULT_ASN = 64512
DEFAULT_DESCRIPTION = "The main transit gateway for the account"
DEFAULT_SHARE_NAME = "transit-gateway-share"

TGW_ID = re.compile(r"tgw-[0-9a-f]+")
ATTACHMENT_ID = re.compile(r"tgw-attach-[0-9a-f]+")
ROUTE_TABLE_ID = re.compile(r"tgw-rtb-[0-9a-f]+")
ACCOUNT_ID = re.compile(r"\d{12}")
ORGANIZATIONS_ARN = re.compile(
    r"arn:aws[a-z-]*:organizations::\d{12}:(?:organization|ou)/o-[a-z0-9]+(?:/ou-[a-z0-9-]+)?"
)

# Marks "the gateway this stack creates" when comparing which gateway things belong to.
CREATED = "<created transit gateway>"


def construct_id(name: str) -> str:
    """`uat-shared` -> `UatShared`, so logical ids stay readable."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[-_]+", name) if part)


def export_suffix(name: str) -> str:
    """Export names allow letters, digits, ':' and '-' only."""
    return name.replace("_", "-")


def _toggle(value: Any, label: str, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "enable" if value else "disable"
    if value not in TOGGLES:
        raise ValueError(f"{label} must be enable, disable, true or false.")
    return value


def _aws_id(value: Any, label: str, pattern: re.Pattern[str], example: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be an id like {example}.")
    return value


def _subnet_type(value: Any, label: str) -> str:
    result = schema.string(value, label, default="private")
    if result not in SUBNET_TYPES:
        raise ValueError(f"{label} must be public or private, got '{result}'.")
    return result


def _cidr(value: Any, label: str) -> str:
    text = schema.required_string(value, label)
    try:
        return str(ipaddress.ip_network(text))
    except ValueError as error:
        raise ValueError(f"{label}: '{text}' is not a valid CIDR ({error}).") from None


def _check_construct_ids(values: Iterable[str], label: str) -> None:
    """`a-b` and `a_b` would become the same logical id."""
    seen: dict[str, str] = {}
    for value in values:
        key = construct_id(value).casefold()
        if key in seen:
            raise ValueError(
                f"{label}: '{value}' and '{seen[key]}' differ only by '-' / '_' or case; "
                "rename one."
            )
        seen[key] = value


def _reference(
    data: Mapping[str, Any], label: str, ref_key: str, id_key: str,
    pattern: re.Pattern[str], example: str,
) -> tuple[str | None, str | None]:
    """A config-name reference and an explicit id are alternatives, never both."""
    ref = data.get(ref_key)
    explicit = data.get(id_key)
    if ref is not None and explicit is not None:
        raise ValueError(f"{label}: give {ref_key} or {id_key}, not both.")
    return (
        schema.name(ref, f"{label}: {ref_key}") if ref is not None else None,
        _aws_id(explicit, f"{label}: {id_key}", pattern, example),
    )


# --- props ----------------------------------------------------------------


@dataclass(frozen=True)
class RamShareProps:
    """Share a gateway this stack creates with other accounts through AWS RAM."""

    share_name: str = DEFAULT_SHARE_NAME
    allow_external_principals: bool = False
    principals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for principal in self.principals:
            if not (ACCOUNT_ID.fullmatch(principal) or ORGANIZATIONS_ARN.fullmatch(principal)):
                raise ValueError(
                    f"transit_gateway.gateway.ram: principal '{principal}' must be a "
                    "12-digit account id or an AWS Organizations organization/OU ARN."
                )
        if len(set(self.principals)) != len(self.principals):
            raise ValueError("transit_gateway.gateway.ram: principals are repeated.")

    @classmethod
    def from_dict(cls, raw: Any) -> RamShareProps | None:
        label = "transit_gateway.gateway.ram"
        data = schema.mapping(
            raw, label, {"enabled", "share_name", "allow_external_principals", "principals"}
        )
        if not schema.boolean(data.get("enabled"), f"{label}: enabled"):
            return None
        return cls(
            share_name=schema.name(
                data.get("share_name", DEFAULT_SHARE_NAME), f"{label}: share_name"
            ),
            allow_external_principals=schema.boolean(
                data.get("allow_external_principals"), f"{label}: allow_external_principals"
            ),
            principals=schema.strings(data.get("principals"), f"{label}: principals"),
        )


@dataclass(frozen=True)
class GatewayProps:
    """Terraform `transit_gateway`: the gateway this stack creates."""

    name: str
    description: str = DEFAULT_DESCRIPTION
    amazon_side_asn: int = DEFAULT_ASN
    default_route_table_association: str = "enable"
    default_route_table_propagation: str = "enable"
    auto_accept_shared_attachments: str = "disable"
    dns_support: str = "enable"
    ram: RamShareProps | None = None

    def __post_init__(self) -> None:
        asn = self.amazon_side_asn
        if not (64512 <= asn <= 65534 or 4200000000 <= asn <= 4294967294):
            raise ValueError(
                f"Transit gateway '{self.name}': amazon_side_asn must be a private ASN, "
                "64512-65534 or 4200000000-4294967294."
            )

    @classmethod
    def from_dict(cls, raw: Any) -> GatewayProps:
        label = "transit_gateway.gateway"
        data = schema.mapping(
            raw, label, {
                "name", "description", "amazon_side_asn", "default_route_table_association",
                "default_route_table_propagation", "auto_accept_shared_attachments",
                "dns_support", "ram",
            }, required=("name",),
        )
        name = schema.name(data["name"], f"{label}: name")
        label = f"Transit gateway '{name}'"
        return cls(
            name=name,
            description=schema.string(
                data.get("description"), f"{label}: description", default=DEFAULT_DESCRIPTION
            ),
            amazon_side_asn=schema.integer(
                data.get("amazon_side_asn"), f"{label}: amazon_side_asn", default=DEFAULT_ASN
            ),
            default_route_table_association=_toggle(
                data.get("default_route_table_association"),
                f"{label}: default_route_table_association", "enable",
            ),
            default_route_table_propagation=_toggle(
                data.get("default_route_table_propagation"),
                f"{label}: default_route_table_propagation", "enable",
            ),
            auto_accept_shared_attachments=_toggle(
                data.get("auto_accept_shared_attachments"),
                f"{label}: auto_accept_shared_attachments", "disable",
            ),
            dns_support=_toggle(data.get("dns_support"), f"{label}: dns_support", "enable"),
            ram=RamShareProps.from_dict(data["ram"]) if data.get("ram") is not None else None,
        )


@dataclass(frozen=True)
class AttachmentProps:
    """Terraform `tgw_attachments`: attach this VPC, one subnet per AZ of a tier."""

    name: str
    subnet_type: str = "private"
    transit_gateway_id: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> AttachmentProps:
        data = schema.mapping(
            raw, "transit_gateway.attachments entry",
            {"name", "subnet_type", "transit_gateway_id"}, required=("name",),
        )
        name = schema.name(data["name"], "transit_gateway.attachments: name")
        label = f"Transit gateway attachment '{name}'"
        return cls(
            name=name,
            subnet_type=_subnet_type(data.get("subnet_type"), f"{label}: subnet_type"),
            transit_gateway_id=_aws_id(
                data.get("transit_gateway_id"), f"{label}: transit_gateway_id", TGW_ID,
                "tgw-0123456789abcdef0",
            ),
        )


@dataclass(frozen=True)
class RouteTableProps:
    """Terraform `tgw_route_table`; `key` defaults to `name`."""

    key: str
    name: str
    transit_gateway_id: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> RouteTableProps:
        data = schema.mapping(
            raw, "transit_gateway.route_tables entry",
            {"key", "name", "transit_gateway_id"}, required=("name",),
        )
        name = schema.name(data["name"], "transit_gateway.route_tables: name")
        key = schema.name(data.get("key", name), "transit_gateway.route_tables: key")
        return cls(
            key=key,
            name=name,
            transit_gateway_id=_aws_id(
                data.get("transit_gateway_id"),
                f"Transit gateway route table '{key}': transit_gateway_id", TGW_ID,
                "tgw-0123456789abcdef0",
            ),
        )


@dataclass(frozen=True)
class LinkProps:
    """An attachment joined to a route table: an association or a propagation."""

    key: str
    attachment: str | None = None
    attachment_id: str | None = None
    route_table: str | None = None
    route_table_id: str | None = None

    @classmethod
    def parse(cls, raw: Any, section: str) -> LinkProps:
        data = schema.mapping(
            raw, f"transit_gateway.{section} entry",
            {"key", "attachment", "attachment_id", "route_table", "route_table_id"},
            required=("key",),
        )
        key = schema.name(data["key"], f"transit_gateway.{section}: key")
        label = f"transit_gateway.{section} '{key}'"
        attachment, attachment_id = _reference(
            data, label, "attachment", "attachment_id", ATTACHMENT_ID,
            "tgw-attach-0123456789abcdef0",
        )
        route_table, route_table_id = _reference(
            data, label, "route_table", "route_table_id", ROUTE_TABLE_ID,
            "tgw-rtb-0123456789abcdef0",
        )
        return cls(key, attachment, attachment_id, route_table, route_table_id)


@dataclass(frozen=True)
class RouteProps:
    """Terraform `tgw_routes`: a route in a transit gateway route table."""

    key: str
    destination_cidr_block: str
    blackhole: bool = False
    attachment: str | None = None
    attachment_id: str | None = None
    route_table: str | None = None
    route_table_id: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> RouteProps:
        data = schema.mapping(
            raw, "transit_gateway.routes entry", {
                "key", "destination_cidr_block", "blackhole", "attachment",
                "attachment_id", "route_table", "route_table_id",
            }, required=("key", "destination_cidr_block"),
        )
        key = schema.name(data["key"], "transit_gateway.routes: key")
        label = f"transit_gateway.routes '{key}'"
        attachment, attachment_id = _reference(
            data, label, "attachment", "attachment_id", ATTACHMENT_ID,
            "tgw-attach-0123456789abcdef0",
        )
        route_table, route_table_id = _reference(
            data, label, "route_table", "route_table_id", ROUTE_TABLE_ID,
            "tgw-rtb-0123456789abcdef0",
        )
        blackhole = schema.boolean(data.get("blackhole"), f"{label}: blackhole")
        if blackhole and (attachment or attachment_id):
            raise ValueError(f"{label}: a blackhole route must not name an attachment.")
        return cls(
            key=key,
            destination_cidr_block=_cidr(
                data["destination_cidr_block"], f"{label}: destination_cidr_block"
            ),
            blackhole=blackhole,
            attachment=attachment,
            attachment_id=attachment_id,
            route_table=route_table,
            route_table_id=route_table_id,
        )


@dataclass(frozen=True)
class SubnetRouteProps:
    """Terraform `tgw_subnet_route`: send a CIDR from VPC subnets to the gateway.

    `availability_zones` holds 0-based AZ indexes; `None` means every subnet in
    the tier.
    """

    name: str
    cidr_block: str
    subnet_type: str = "private"
    availability_zones: tuple[int, ...] | None = None
    transit_gateway_id: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> SubnetRouteProps:
        data = schema.mapping(
            raw, "transit_gateway.subnet_routes entry",
            {"name", "cidr_block", "subnet_type", "availability_zones", "transit_gateway_id"},
            required=("name", "cidr_block"),
        )
        name = schema.name(data["name"], "transit_gateway.subnet_routes: name")
        label = f"Transit gateway subnet route '{name}'"
        cidr = _cidr(data["cidr_block"], f"{label}: cidr_block")
        if ipaddress.ip_network(cidr).prefixlen == 0:
            raise ValueError(
                f"{label}: {cidr} would replace the subnet's internet/NAT default route; "
                "route specific ranges to the transit gateway instead."
            )
        return cls(
            name=name,
            cidr_block=cidr,
            subnet_type=_subnet_type(data.get("subnet_type"), f"{label}: subnet_type"),
            availability_zones=cls._zones(data.get("availability_zones"), label),
            transit_gateway_id=_aws_id(
                data.get("transit_gateway_id"), f"{label}: transit_gateway_id", TGW_ID,
                "tgw-0123456789abcdef0",
            ),
        )

    @staticmethod
    def _zones(raw: Any, label: str) -> tuple[int, ...] | None:
        if raw is None:
            return None
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"{label}: availability_zones must be a nonempty list.")
        zones: list[int] = []
        for value in raw:
            if isinstance(value, str) and value in ORDINALS:
                index = ORDINALS.index(value)
            elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value < MAX_AZS:
                index = value
            else:
                raise ValueError(
                    f"{label}: availability zone '{value}' must be one of "
                    f"{', '.join(ORDINALS)} or an index 0-{MAX_AZS - 1}."
                )
            if index in zones:
                raise ValueError(f"{label}: availability zone '{value}' is repeated.")
            zones.append(index)
        return tuple(zones)


@dataclass(frozen=True)
class TransitGatewayProps:
    """Everything under `transit_gateway:`; cross-references are checked here."""

    gateway: GatewayProps | None = None
    transit_gateway_id: str | None = None
    attachments: tuple[AttachmentProps, ...] = ()
    route_tables: tuple[RouteTableProps, ...] = ()
    associations: tuple[LinkProps, ...] = ()
    propagations: tuple[LinkProps, ...] = ()
    routes: tuple[RouteProps, ...] = ()
    subnet_routes: tuple[SubnetRouteProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.gateway or self.attachments or self.route_tables or self.associations
            or self.propagations or self.routes or self.subnet_routes
        )

    def __post_init__(self) -> None:
        if self.gateway and self.transit_gateway_id:
            raise ValueError(
                "transit_gateway: give gateway (create one) or transit_gateway_id "
                "(use an existing one), not both."
            )
        _check_construct_ids((a.name for a in self.attachments), "transit_gateway.attachments")
        _check_construct_ids((t.key for t in self.route_tables), "transit_gateway.route_tables")
        _check_construct_ids((a.key for a in self.associations), "transit_gateway.associations")
        _check_construct_ids((p.key for p in self.propagations), "transit_gateway.propagations")
        _check_construct_ids((r.key for r in self.routes), "transit_gateway.routes")
        _check_construct_ids((s.name for s in self.subnet_routes), "transit_gateway.subnet_routes")

        attached: dict[str, str] = {}
        for attachment in self.attachments:
            target = self.target(
                attachment.transit_gateway_id, f"Transit gateway attachment '{attachment.name}'"
            )
            if target in attached:
                raise ValueError(
                    f"Transit gateway attachments '{attached[target]}' and "
                    f"'{attachment.name}' attach the same VPC to the same gateway."
                )
            attached[target] = attachment.name
        for table in self.route_tables:
            self.target(table.transit_gateway_id, f"Transit gateway route table '{table.key}'")
        for route in self.subnet_routes:
            self.target(route.transit_gateway_id, f"Transit gateway subnet route '{route.name}'")

        associated: dict[str, str] = {}
        for link in self.associations:
            label = f"transit_gateway.associations '{link.key}'"
            self._check_link(link, label)
            attachment = link.attachment or link.attachment_id
            if attachment in associated:
                raise ValueError(
                    f"{label}: attachment '{attachment}' is already associated by "
                    f"'{associated[attachment]}'; an attachment has one route table."
                )
            associated[attachment] = link.key
            if (
                link.attachment and self.gateway
                and self.gateway.default_route_table_association == "enable"
                and self._attachment_target(link.attachment) == CREATED
            ):
                raise ValueError(
                    f"{label}: the gateway associates new attachments with its default "
                    "route table, so this association would fail. Set "
                    "gateway.default_route_table_association: disable."
                )

        propagated: dict[tuple[str, str], str] = {}
        for link in self.propagations:
            label = f"transit_gateway.propagations '{link.key}'"
            self._check_link(link, label)
            pair = (link.attachment or link.attachment_id, link.route_table or link.route_table_id)
            if pair in propagated:
                raise ValueError(f"{label}: repeats propagation '{propagated[pair]}'.")
            propagated[pair] = link.key

        destinations: dict[tuple[str, str], str] = {}
        for route in self.routes:
            label = f"transit_gateway.routes '{route.key}'"
            if not route.blackhole and not (route.attachment or route.attachment_id):
                raise ValueError(f"{label}: needs attachment or attachment_id, or blackhole: true.")
            self._check_link(route, label, attachment_required=not route.blackhole)
            destination = (route.route_table or route.route_table_id, route.destination_cidr_block)
            if destination in destinations:
                raise ValueError(
                    f"{label}: route table '{destination[0]}' already routes "
                    f"{destination[1]} in '{destinations[destination]}'."
                )
            destinations[destination] = route.key

    # --- references ---------------------------------------------------------

    def target(self, transit_gateway_id: str | None, label: str) -> str:
        """Which gateway an item belongs to: its own id, or the stack's gateway."""
        if transit_gateway_id:
            return transit_gateway_id
        if self.gateway:
            return CREATED
        if self.transit_gateway_id:
            return self.transit_gateway_id
        raise ValueError(
            f"{label}: no transit gateway. Set transit_gateway.gateway to create one, "
            "transit_gateway.transit_gateway_id to use an existing one, or "
            "transit_gateway_id on this entry."
        )

    def attachment_props(self, name: str, label: str) -> AttachmentProps:
        return schema.lookup({a.name: a for a in self.attachments}, name, f"{label}: attachment")

    def route_table_props(self, key: str, label: str) -> RouteTableProps:
        return schema.lookup({t.key: t for t in self.route_tables}, key, f"{label}: route_table")

    def _attachment_target(self, name: str) -> str:
        attachment = self.attachment_props(name, "transit_gateway")
        return self.target(attachment.transit_gateway_id, f"Transit gateway attachment '{name}'")

    def _check_link(
        self, link: LinkProps | RouteProps, label: str, *, attachment_required: bool = True
    ) -> None:
        if attachment_required and not (link.attachment or link.attachment_id):
            raise ValueError(f"{label}: needs attachment or attachment_id.")
        if not (link.route_table or link.route_table_id):
            raise ValueError(f"{label}: needs route_table or route_table_id.")
        if link.attachment:
            self.attachment_props(link.attachment, label)
        if link.route_table:
            self.route_table_props(link.route_table, label)
        if link.attachment and link.route_table:
            table = self.route_table_props(link.route_table, label)
            attachment_target = self._attachment_target(link.attachment)
            table_target = self.target(table.transit_gateway_id, label)
            if attachment_target != table_target:
                raise ValueError(
                    f"{label}: attachment '{link.attachment}' and route table "
                    f"'{link.route_table}' belong to different transit gateways."
                )

    # --- parsing ------------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> TransitGatewayProps:
        del base_dir  # nothing in this family is file-backed
        if raw is None:
            return cls()
        label = "transit_gateway"
        data = schema.mapping(raw, label, {
            "gateway", "transit_gateway_id", "attachments", "route_tables", "associations",
            "propagations", "routes", "subnet_routes",
        })
        attachments = schema.items(
            data.get("attachments"), f"{label}.attachments", AttachmentProps.from_dict
        )
        route_tables = schema.items(
            data.get("route_tables"), f"{label}.route_tables", RouteTableProps.from_dict,
            key=lambda table: table.key,
        )

        def links(section: str) -> tuple[LinkProps, ...]:
            return schema.items(
                data.get(section), f"{label}.{section}",
                lambda item: LinkProps.parse(item, section), key=lambda link: link.key,
            )

        def defaults(item: Any, *, attachment: bool = True) -> Any:
            """Terraform falls back to the only attachment and the only route table."""
            changes: dict[str, str] = {}
            if attachment and not (item.attachment or item.attachment_id) and len(attachments) == 1:
                changes["attachment"] = attachments[0].name
            if not (item.route_table or item.route_table_id) and len(route_tables) == 1:
                changes["route_table"] = route_tables[0].key
            return replace(item, **changes)

        routes = schema.items(
            data.get("routes"), f"{label}.routes", RouteProps.from_dict,
            key=lambda route: route.key,
        )
        return cls(
            gateway=(
                GatewayProps.from_dict(data["gateway"]) if data.get("gateway") is not None
                else None
            ),
            transit_gateway_id=_aws_id(
                data.get("transit_gateway_id"), f"{label}.transit_gateway_id", TGW_ID,
                "tgw-0123456789abcdef0",
            ),
            attachments=attachments,
            route_tables=route_tables,
            associations=tuple(defaults(link) for link in links("associations")),
            propagations=tuple(defaults(link) for link in links("propagations")),
            routes=tuple(defaults(route, attachment=not route.blackhole) for route in routes),
            subnet_routes=schema.items(
                data.get("subnet_routes"), f"{label}.subnet_routes", SubnetRouteProps.from_dict
            ),
        )


# --- resources ------------------------------------------------------------


class TransitGateway(Construct):
    """Builds the gateway (or adopts an existing id) and everything wired to it."""

    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        common: Common,
        network_name: str,
        props: TransitGatewayProps,
        network: Network,
    ) -> None:
        super().__init__(scope, construct_id_)
        self.common = common
        self.network_name = network_name
        self.props = props
        self.network = network

        self.gateway: ec2.CfnTransitGateway | None = None
        self.transit_gateway_id: str | None = props.transit_gateway_id
        self.transit_gateway_arn: str | None = None
        self.resource_share: ram.CfnResourceShare | None = None
        if props.gateway:
            self._add_gateway(props.gateway)

        self.attachments = {a.name: self._add_attachment(a) for a in props.attachments}
        self.route_tables = {t.key: self._add_route_table(t) for t in props.route_tables}
        self.associations = {
            link.key: ec2.CfnTransitGatewayRouteTableAssociation(
                self, f"Association{construct_id(link.key)}",
                transit_gateway_attachment_id=self._attachment_id(link),
                transit_gateway_route_table_id=self._route_table_id(link),
            )
            for link in props.associations
        }
        self.propagations = {
            link.key: ec2.CfnTransitGatewayRouteTablePropagation(
                self, f"Propagation{construct_id(link.key)}",
                transit_gateway_attachment_id=self._attachment_id(link),
                transit_gateway_route_table_id=self._route_table_id(link),
            )
            for link in props.propagations
        }
        self.routes = {route.key: self._add_route(route) for route in props.routes}
        self.subnet_routes: dict[str, tuple[ec2.CfnRoute, ...]] = {}
        destinations: dict[tuple[str, int, str], str] = {}
        for route in props.subnet_routes:
            self.subnet_routes[route.name] = self._add_subnet_routes(route, destinations)

    def _name(self, *parts: str) -> str:
        return self.common.name(self.network_name, *parts)

    def _gateway_id(self, transit_gateway_id: str | None) -> str:
        return transit_gateway_id or self.transit_gateway_id  # type: ignore[return-value]

    # --- gateway --------------------------------------------------------------

    def _add_gateway(self, props: GatewayProps) -> None:
        name = self._name(props.name, "tgw")
        gateway = ec2.CfnTransitGateway(
            self, "Gateway",
            description=props.description,
            amazon_side_asn=props.amazon_side_asn,
            default_route_table_association=props.default_route_table_association,
            default_route_table_propagation=props.default_route_table_propagation,
            auto_accept_shared_attachments=props.auto_accept_shared_attachments,
            dns_support=props.dns_support,
        )
        cdk.Tags.of(gateway).add("Name", name)
        self.gateway = gateway
        self.transit_gateway_id = gateway.ref
        self.transit_gateway_arn = cdk.Stack.of(self).format_arn(
            service="ec2", resource="transit-gateway", resource_name=gateway.ref
        )
        if props.ram:
            share_name = self._name(props.ram.share_name)
            self.resource_share = ram.CfnResourceShare(
                self, "ResourceShare",
                name=share_name,
                allow_external_principals=props.ram.allow_external_principals,
                principals=list(props.ram.principals) or None,
                resource_arns=[self.transit_gateway_arn],
            )
            cdk.Tags.of(self.resource_share).add("Name", share_name)

    # --- attachments and route tables -------------------------------------------

    def _subnets(self, subnet_type: str, label: str) -> list[ec2.ISubnet]:
        subnets = (
            self.network.private_subnets if subnet_type == "private"
            else self.network.public_subnets
        )
        if not subnets:
            raise ValueError(
                f"{label}: subnet_type is {subnet_type}, but network '{self.network_name}' "
                f"has no {subnet_type} subnets. Set network.private_subnets: true or use "
                "subnet_type: public."
            )
        return list(subnets)

    def _add_attachment(
        self, props: AttachmentProps
    ) -> ec2.CfnTransitGatewayVpcAttachment:
        # Network builds exactly one subnet per AZ per tier, which is what a
        # transit gateway attachment allows.
        subnets = self._subnets(props.subnet_type, f"Transit gateway attachment '{props.name}'")
        attachment = ec2.CfnTransitGatewayVpcAttachment(
            self, f"Attachment{construct_id(props.name)}",
            transit_gateway_id=self._gateway_id(props.transit_gateway_id),
            vpc_id=self.network.vpc.vpc_id,
            subnet_ids=[subnet.subnet_id for subnet in subnets],
        )
        cdk.Tags.of(attachment).add("Name", self._name(props.name, "tgw-attach"))
        return attachment

    def _add_route_table(self, props: RouteTableProps) -> ec2.CfnTransitGatewayRouteTable:
        table = ec2.CfnTransitGatewayRouteTable(
            self, f"RouteTable{construct_id(props.key)}",
            transit_gateway_id=self._gateway_id(props.transit_gateway_id),
        )
        cdk.Tags.of(table).add("Name", self._name(props.name, "tgw-rtb"))
        return table

    def _attachment_id(self, link: LinkProps | RouteProps) -> str | None:
        if link.attachment:
            return self.attachments[link.attachment].ref
        return link.attachment_id

    def _route_table_id(self, link: LinkProps | RouteProps) -> str:
        if link.route_table:
            return self.route_tables[link.route_table].ref
        return link.route_table_id  # type: ignore[return-value]

    # --- routes -----------------------------------------------------------------

    def _add_route(self, props: RouteProps) -> ec2.CfnTransitGatewayRoute:
        return ec2.CfnTransitGatewayRoute(
            self, f"Route{construct_id(props.key)}",
            transit_gateway_route_table_id=self._route_table_id(props),
            destination_cidr_block=props.destination_cidr_block,
            blackhole=props.blackhole,
            transit_gateway_attachment_id=None if props.blackhole else self._attachment_id(props),
        )

    def _add_subnet_routes(
        self, props: SubnetRouteProps, destinations: dict[tuple[str, int, str], str]
    ) -> tuple[ec2.CfnRoute, ...]:
        label = f"Transit gateway subnet route '{props.name}'"
        subnets = self._subnets(props.subnet_type, label)
        zones = props.availability_zones
        if zones is None:
            zones = tuple(range(len(subnets)))
        target = self.props.target(props.transit_gateway_id, label)
        gateway_id = self._gateway_id(props.transit_gateway_id)
        ipv6 = ipaddress.ip_network(props.cidr_block).version == 6

        routes = []
        for index in zones:
            if index >= len(subnets):
                raise ValueError(
                    f"{label}: availability zone {ordinal(index)} (index {index}) does not "
                    f"exist; network '{self.network_name}' has {len(subnets)} "
                    f"{props.subnet_type} subnets."
                )
            key = (props.subnet_type, index, props.cidr_block)
            if key in destinations:
                raise ValueError(
                    f"{label}: the {ordinal(index)} {props.subnet_type} route table already "
                    f"routes {props.cidr_block} in '{destinations[key]}'."
                )
            destinations[key] = props.name
            route = ec2.CfnRoute(
                self, f"SubnetRoute{construct_id(props.name)}{ordinal(index).capitalize()}",
                route_table_id=subnets[index].route_table.route_table_id,
                transit_gateway_id=gateway_id,
                destination_cidr_block=None if ipv6 else props.cidr_block,
                destination_ipv6_cidr_block=props.cidr_block if ipv6 else None,
            )
            # A VPC route can only point at a gateway once the VPC is attached to it.
            for attachment in self.props.attachments:
                if self.props.target(attachment.transit_gateway_id, label) == target:
                    route.add_resource_dependency(self.attachments[attachment.name])
            routes.append(route)
        return tuple(routes)
