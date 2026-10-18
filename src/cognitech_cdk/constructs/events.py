"""EventBridge rules (and custom buses), mirroring the Terraform `Event-Bridge` module.

    events:
      - name: eks-node-tagger-rule
        description: Tag EKS nodes when they start
        event_pattern:                    # mapping, JSON string, or repo-relative file
          source: [aws.ec2]
          detail-type: [EC2 Instance State-change Notification]
          detail:
            state: [running]
        target: eks-node-tagger           # a function from `lambdas`, or target_arn
        event_bus: default                # optional; any other name creates a bus
        enabled: true
        tags:
          Used_for: eks-node-tagging

A Lambda target gets an `AWS::Lambda::Permission` for `events.amazonaws.com`
scoped to the rule's ARN, so a separate `lambda_invocations` entry is not
needed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from cognitech_cdk.common import schema
from cognitech_cdk.common.documents import json_compatible, load_document
from cognitech_cdk.constructs.lambdas import check_unique_ids

TARGET_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{0,12}:.+")
DEFAULT_BUS = "default"


def _event_pattern(raw: Any, label: str, *, base_dir: Path | None) -> dict[str, Any]:
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"{label}: invalid JSON: {error}") from error
    document = load_document(raw, label, base_dir=base_dir)
    if not isinstance(document, Mapping) or not document:
        raise ValueError(f"{label} must be a nonempty mapping.")
    return json_compatible(document, label)


@dataclass(frozen=True)
class RuleProps:
    name: str
    event_pattern: Mapping[str, Any]
    target: str | None = None
    target_arn: str | None = None
    description: str | None = None
    enabled: bool = True
    event_bus: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> RuleProps:
        data = schema.mapping(
            raw, "events entry",
            {
                "name", "description", "event_pattern", "enabled", "event_bus",
                "target", "target_arn", "tags",
            },
            required=("name", "event_pattern"),
        )
        name = schema.name(data["name"], "events name")
        label = f"Event rule '{name}'"
        target = data.get("target")
        target_arn = data.get("target_arn")
        if (target is None) == (target_arn is None):
            raise ValueError(f"{label}: provide exactly one of target or target_arn.")
        if target is not None:
            schema.name(target, f"{label}.target")
        if target_arn is not None and (
            not isinstance(target_arn, str) or not TARGET_ARN.fullmatch(target_arn)
        ):
            raise ValueError(f"{label}.target_arn must be an ARN.")
        description = schema.string(data.get("description"), f"{label}.description")
        if description is not None and len(description) > 512:
            raise ValueError(f"{label}.description must be at most 512 characters.")
        bus = schema.string(data.get("event_bus"), f"{label}.event_bus", allow_empty=True)
        if bus in ("", DEFAULT_BUS):
            bus = None
        if bus is not None:
            schema.name(bus, f"{label}.event_bus")
        return cls(
            name=name,
            event_pattern=_event_pattern(
                data["event_pattern"], f"{label}.event_pattern", base_dir=base_dir
            ),
            target=target,
            target_arn=target_arn,
            description=description,
            enabled=schema.boolean(data.get("enabled"), f"{label}.enabled", default=True),
            event_bus=bus,
            tags=schema.string_map(data.get("tags"), f"{label}.tags"),
        )


@dataclass(frozen=True)
class EventsProps:
    rules: tuple[RuleProps, ...] = ()

    def __post_init__(self) -> None:
        check_unique_ids([rule.name for rule in self.rules], "events")
        check_unique_ids(list(self.buses), "events event_bus")

    def __bool__(self) -> bool:
        return bool(self.rules)

    @property
    def buses(self) -> tuple[str, ...]:
        """Custom buses, once each, in first-use order."""
        return tuple(dict.fromkeys(rule.event_bus for rule in self.rules if rule.event_bus))

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EventsProps:
        """Parse the `events:` list; `None` is empty."""
        return cls(
            rules=schema.items(
                raw, "events", lambda item: RuleProps.from_dict(item, base_dir=base_dir)
            )
        )
