"""EventBridge buses and rules, with invoke permissions for Lambda targets."""

from __future__ import annotations

import re
from typing import Mapping

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.events import EventsProps, RuleProps
from cognitech_cdk.constructs.lambdas import construct_id as logical_id
from cognitech_cdk.constructs.lambdas import export_key
from cognitech_cdk.stacks.base import EnvironmentStack

RULE_NAME = re.compile(r"[.\-_A-Za-z0-9]{1,64}")
BUS_NAME = re.compile(r"[/.\-_A-Za-z0-9]{1,256}")


class EventsStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: EventsProps,
        functions: Mapping[str, lambda_.IFunction] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        self.props = props
        self._functions = functions or {}

        self.buses: dict[str, events.IEventBus] = {}
        self.rules: dict[str, events.IRule] = {}
        self.cfn_rules: dict[str, events.CfnRule] = {}
        self.permissions: dict[str, lambda_.CfnPermission] = {}

        for bus in props.buses:
            self._add_bus(bus)
        for rule in props.rules:
            self._add_rule(rule)

    def _add_bus(self, bus_name: str) -> None:
        name = self.resource_name(bus_name)
        if not BUS_NAME.fullmatch(name):
            raise ValueError(f"Event bus '{bus_name}': name '{name}' is too long or invalid.")
        cid = logical_id(bus_name)
        bus = events.EventBus(self, f"{cid}EventBus", event_bus_name=name)
        cdk.Tags.of(bus).add("Name", name)
        self.buses[bus_name] = bus
        key = export_key(bus_name)
        self.output(f"{cid}EventBusArn", bus.event_bus_arn, f"{key}-event-bus-arn")
        self.output(f"{cid}EventBusName", bus.event_bus_name, f"{key}-event-bus-name")

    def _add_rule(self, props: RuleProps) -> None:
        label = f"Event rule '{props.name}'"
        name = self.resource_name(props.name)
        if not RULE_NAME.fullmatch(name):
            raise ValueError(
                f"{label}: rule name '{name}' must be at most 64 letters, digits, "
                "'.', '-' or '_'."
            )
        cid = logical_id(props.name)
        function = (
            schema.lookup(self._functions, props.target, f"{label}.target")
            if props.target is not None else None
        )
        target_arn = function.function_arn if function is not None else props.target_arn
        bus = self.buses[props.event_bus] if props.event_bus else None

        rule = events.CfnRule(
            self,
            f"{cid}Rule",
            name=name,
            description=props.description,
            event_bus_name=bus.event_bus_name if bus is not None else None,
            event_pattern=dict(props.event_pattern),
            state="ENABLED" if props.enabled else "DISABLED",
            targets=[events.CfnRule.TargetProperty(id="Target0", arn=target_arn)],
        )
        cdk.Tags.of(rule).add("Name", name)
        for key, value in props.tags.items():
            cdk.Tags.of(rule).add(key, value)

        if function is not None:
            self.permissions[props.name] = lambda_.CfnPermission(
                self,
                f"{cid}InvokePermission",
                action="lambda:InvokeFunction",
                function_name=function.function_arn,
                principal="events.amazonaws.com",
                source_arn=rule.attr_arn,
            )

        self.cfn_rules[props.name] = rule
        self.rules[props.name] = events.Rule.from_event_rule_arn(
            self, f"{cid}RuleReference", rule.attr_arn
        )
        key = export_key(props.name)
        self.output(f"{cid}RuleArn", rule.attr_arn, f"{key}-rule-arn")
        self.output(f"{cid}RuleName", name, f"{key}-rule-name")
