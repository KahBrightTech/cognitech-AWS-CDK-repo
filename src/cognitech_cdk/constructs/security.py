"""Security groups and their rules, built from `deployments/<env>/env.yaml`.

Every group is declared in config rather than in code, so adding one for a new
resource — a cluster, a database, a load balancer — is a YAML change:

    security_groups:
      - name: eks-cluster
        description: EKS control plane
        ingress:
          - description: API server from the nodes
            source: eks-nodes          # another group in this list
            ports: 443
      - name: eks-nodes
        description: EKS worker nodes
        ingress:
          - description: kubelet from the control plane
            source: eks-cluster
            ports: 10250
          - description: node to node
            source: eks-nodes          # a group may reference itself
            protocol: all

A rule's `source` (ingress) or `destination` (egress) is one of:

    eks-nodes          another group in this list, or the group itself
    10.20.0.0/16       an IPv4 range
    2600:1f18::/32     an IPv6 range
    sg-0abc123         a group that already exists outside this stack
    pl-0abc123         a prefix list

Outbound defaults to "allow all" unless `egress` is listed, in which case the
listed rules are the only way out. Set `allow_all_outbound: false` with no
`egress` for a group that may not talk to anything.

Resource names follow the convention used elsewhere in this repo:

    {account_name}-{region_prefix}-{name}-sg
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from cognitech_cdk.common.config import Common

PROTOCOLS = ("tcp", "udp", "icmp", "all")
PORTLESS = ("icmp", "all")
MAX_PORT = 65535

# A rule either points at a CIDR / existing id, or names a group in this config.
CIDR = re.compile(r"^[0-9a-fA-F.:]+/\d{1,3}$")
EXISTING_GROUP = re.compile(r"^sg-[0-9a-f]+$")
PREFIX_LIST = re.compile(r"^pl-[0-9a-f]+$")


def _construct_id(name: str) -> str:
    """`eks-cluster` -> `EksCluster`, so logical ids stay readable."""
    return "".join(part.capitalize() for part in re.split(r"[-_\s]+", name) if part)


@dataclass(frozen=True)
class RuleProps:
    """One ingress or egress rule."""

    peer: str
    description: str
    protocol: str = "tcp"
    ports: str | None = None

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValueError(
                f"Rule '{self.description}': protocol must be one of "
                f"{', '.join(PROTOCOLS)}, got '{self.protocol}'."
            )
        if self.protocol in PORTLESS:
            if self.ports is not None:
                raise ValueError(
                    f"Rule '{self.description}': protocol '{self.protocol}' covers every "
                    f"port, so 'ports' must be left out."
                )
            self._check_peer()
            return
        if self.ports is None:
            raise ValueError(
                f"Rule '{self.description}': {self.protocol} rules need 'ports', "
                f"e.g. 443 or 30000-32767."
            )
        self.port_range()
        self._check_peer()

    def _check_peer(self) -> None:
        """A CIDR is parsed now so a typo fails here, not half way through a deploy."""
        if not CIDR.match(self.peer):
            return
        try:
            ipaddress.ip_network(self.peer)
        except ValueError as error:
            raise ValueError(
                f"Rule '{self.description}': '{self.peer}' is not a valid CIDR ({error})."
            ) from None

    def port_range(self) -> tuple[int, int]:
        """`(low, high)`, equal for a single port."""
        text = str(self.ports).strip()
        low, _, high = text.partition("-")
        high = high or low
        try:
            bounds = (int(low), int(high))
        except ValueError:
            raise ValueError(
                f"Rule '{self.description}': ports must be a number or a range "
                f"like 30000-32767, got '{self.ports}'."
            ) from None
        for value in bounds:
            if not 0 <= value <= MAX_PORT:
                raise ValueError(
                    f"Rule '{self.description}': port {value} is outside 0-{MAX_PORT}."
                )
        if bounds[0] > bounds[1]:
            raise ValueError(
                f"Rule '{self.description}': port range '{self.ports}' runs backwards."
            )
        return bounds

    @property
    def references_group(self) -> bool:
        """True when the peer is a group name rather than a CIDR or an id."""
        return not (
            CIDR.match(self.peer)
            or EXISTING_GROUP.match(self.peer)
            or PREFIX_LIST.match(self.peer)
        )

    def port(self) -> ec2.Port:
        if self.protocol == "all":
            return ec2.Port.all_traffic()
        if self.protocol == "icmp":
            return ec2.Port.all_icmp()

        low, high = self.port_range()
        if self.protocol == "udp":
            return ec2.Port.udp(low) if low == high else ec2.Port.udp_range(low, high)
        return ec2.Port.tcp(low) if low == high else ec2.Port.tcp_range(low, high)

    def identity(self) -> tuple[str, str, str | None]:
        """What makes a rule a duplicate: the same traffic from the same place."""
        return (self.peer, self.protocol, str(self.ports) if self.ports else None)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, peer_key: str, group: str) -> "RuleProps":
        peer = raw.get(peer_key)
        if not peer:
            raise ValueError(
                f"Security group '{group}': every {peer_key.replace('destination', 'egress')} "
                f"rule needs a '{peer_key}'."
            )
        ports = raw.get("ports")
        return cls(
            peer=str(peer),
            description=str(raw.get("description") or f"{peer_key} {peer}"),
            protocol=str(raw.get("protocol", "tcp")).lower(),
            ports=str(ports) if ports is not None else None,
        )


@dataclass(frozen=True)
class SecurityGroupProps:
    """One security group and the rules attached to it."""

    name: str
    description: str
    ingress: tuple[RuleProps, ...] = ()
    egress: tuple[RuleProps, ...] = ()
    allow_all_outbound: bool | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("A security group needs a name.")
        if self.allow_all_outbound and self.egress:
            raise ValueError(
                f"Security group '{self.name}': allow_all_outbound is true, which would "
                f"make the listed egress rules meaningless. Drop one or the other."
            )
        for direction, rules in (("ingress", self.ingress), ("egress", self.egress)):
            seen: dict[tuple[str, str, str | None], str] = {}
            for rule in rules:
                key = rule.identity()
                if key in seen:
                    raise ValueError(
                        f"Security group '{self.name}': duplicate {direction} rule "
                        f"'{rule.description}' repeats '{seen[key]}'."
                    )
                seen[key] = rule.description

    @property
    def allows_all_outbound(self) -> bool:
        """Listing egress rules makes them the only way out."""
        if self.allow_all_outbound is not None:
            return self.allow_all_outbound
        return not self.egress

    def referenced_groups(self) -> set[str]:
        return {
            rule.peer
            for rule in (*self.ingress, *self.egress)
            if rule.references_group
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SecurityGroupProps":
        name = str(raw.get("name") or "")
        if not name:
            raise ValueError("Every entry in security_groups needs a 'name'.")

        def rules(key: str, peer_key: str) -> tuple[RuleProps, ...]:
            return tuple(
                RuleProps.from_dict(entry, peer_key=peer_key, group=name)
                for entry in (raw.get(key) or [])
            )

        allow_all = raw.get("allow_all_outbound")
        return cls(
            name=name,
            description=str(raw.get("description") or f"Security group for {name}"),
            ingress=rules("ingress", "source"),
            egress=rules("egress", "destination"),
            allow_all_outbound=bool(allow_all) if allow_all is not None else None,
            tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
        )

    @classmethod
    def load_all(cls, raw: Any) -> tuple["SecurityGroupProps", ...]:
        """Parse the whole `security_groups:` list and check it hangs together.

        References are resolved here rather than at synth time so a typo fails
        with a readable message before anything is built.
        """
        if raw is None:
            return ()
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError("security_groups must be a list of groups.")

        groups = tuple(cls.from_dict(entry) for entry in raw)

        seen: set[str] = set()
        for group in groups:
            if group.name in seen:
                raise ValueError(f"security_groups: '{group.name}' is defined twice.")
            seen.add(group.name)

        for group in groups:
            for reference in sorted(group.referenced_groups()):
                if reference not in seen:
                    raise ValueError(
                        f"Security group '{group.name}' points at '{reference}', which is "
                        f"not a group in this environment, a CIDR, an sg- id or a pl- id. "
                        f"Known groups: {', '.join(sorted(seen))}."
                    )
        return groups


class SecurityGroups(Construct):
    """Builds every configured group, then wires the rules between them.

    Groups are created before any rule is added so they can reference each
    other in any order, including mutually. A rule whose peer is another group
    becomes its own `AWS::EC2::SecurityGroupIngress` resource rather than an
    inline rule, which is what keeps a cluster/node pair from deadlocking on
    each other during a deploy.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        common: Common,
        props: Sequence[SecurityGroupProps],
        vpc: ec2.IVpc,
    ) -> None:
        super().__init__(scope, construct_id)

        self.common = common
        self.props = tuple(props)
        self.vpc = vpc
        self.groups: dict[str, ec2.SecurityGroup] = {}

        for group in self.props:
            self.groups[group.name] = self._add_group(group)
        for group in self.props:
            self._add_rules(group)

    def __getitem__(self, name: str) -> ec2.SecurityGroup:
        """`security_groups["eks-nodes"]`, for passing to a cluster or instance."""
        try:
            return self.groups[name]
        except KeyError:
            known = ", ".join(sorted(self.groups)) or "none"
            raise KeyError(f"No security group '{name}'. Configured: {known}.") from None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(group.name for group in self.props)

    # --- resources --------------------------------------------------------

    def _add_group(self, props: SecurityGroupProps) -> ec2.SecurityGroup:
        full_name = self.common.name(props.name, "sg")
        group = ec2.SecurityGroup(
            self,
            _construct_id(props.name),
            vpc=self.vpc,
            security_group_name=full_name,
            description=props.description,
            allow_all_outbound=props.allows_all_outbound,
        )
        cdk.Tags.of(group).add("Name", full_name)
        for key, value in props.tags.items():
            cdk.Tags.of(group).add(key, value)
        return group

    def _add_rules(self, props: SecurityGroupProps) -> None:
        group = self.groups[props.name]
        for rule in props.ingress:
            group.add_ingress_rule(self._peer(rule), rule.port(), rule.description)
        for rule in props.egress:
            group.add_egress_rule(self._peer(rule), rule.port(), rule.description)

    def _peer(self, rule: RuleProps) -> ec2.IPeer:
        if rule.references_group:
            return self.groups[rule.peer]
        if EXISTING_GROUP.match(rule.peer):
            return ec2.Peer.security_group_id(rule.peer)
        if PREFIX_LIST.match(rule.peer):
            return ec2.Peer.prefix_list(rule.peer)
        if ipaddress.ip_network(rule.peer).version == 6:
            return ec2.Peer.ipv6(rule.peer)
        return ec2.Peer.ipv4(rule.peer)
