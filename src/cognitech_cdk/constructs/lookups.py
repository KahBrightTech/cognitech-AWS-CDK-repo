"""Deploy-time lookups for values CDK cannot know during credential-free synth."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

_UUID = "8f0f3f8a-4f44-4f8c-b2c6-1d8c3f9fd4f1"
_CODE = r'''
import json
import re
import urllib.request

import boto3


def _send(event, context, status, value=None, reason=None):
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch log stream {context.log_stream_name}",
        "PhysicalResourceId": value or event.get("PhysicalResourceId") or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": {"Value": value} if value else {},
    }).encode()
    request = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    urllib.request.urlopen(request, timeout=10).read()


def _hosted_zone(props):
    name = props["ZoneName"].rstrip(".") + "."
    private = props.get("Private") == "true"
    client = boto3.client("route53")
    zones = []
    marker = {}
    while True:
        page = client.list_hosted_zones_by_name(DNSName=name, MaxItems="100", **marker)
        for zone in page["HostedZones"]:
            if zone["Name"] == name and zone["Config"]["PrivateZone"] == private:
                zones.append(zone["Id"].rsplit("/", 1)[-1])
            elif zone["Name"] > name:
                break
        if not page.get("IsTruncated") or page.get("NextDNSName") != name:
            break
        marker = {"HostedZoneId": page["NextHostedZoneId"]}
    if len(zones) != 1:
        raise ValueError(f"expected one {'private' if private else 'public'} zone {name}, found {len(zones)}")
    return zones[0]


def _sso_role(props):
    pattern = re.compile(rf"^AWSReservedSSO_{re.escape(props['PermissionSet'])}_[0-9a-f]{{16}}$")
    roles = []
    paginator = boto3.client("iam").get_paginator("list_roles")
    for page in paginator.paginate(PathPrefix="/aws-reserved/sso.amazonaws.com/"):
        roles += [role["Arn"] for role in page["Roles"] if pattern.match(role["RoleName"])]
    if len(roles) != 1:
        raise ValueError(f"expected one SSO role for {props['PermissionSet']}, found {len(roles)}")
    return roles[0]


def handler(event, context):
    try:
        if event["RequestType"] == "Delete":
            _send(event, context, "SUCCESS")
            return
        props = event["ResourceProperties"]
        value = {"HostedZone": _hosted_zone, "SsoRole": _sso_role}[props["Kind"]](props)
        _send(event, context, "SUCCESS", value)
    except Exception as error:
        _send(event, context, "FAILED", reason=str(error)[:900])
'''


class DeployTimeLookup(Construct):
    """A read-only CloudFormation custom resource whose `value` is a token."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kind: str,
        properties: dict[str, str],
        actions: list[str],
    ) -> None:
        super().__init__(scope, construct_id)
        function = lambda_.SingletonFunction(
            self,
            "Function",
            uuid=_UUID,
            lambda_purpose="DeployTimeLookup",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline(_CODE),
            timeout=cdk.Duration.minutes(1),
            description="Read-only lookups for values unavailable during CDK synth",
        )
        function.add_to_role_policy(iam.PolicyStatement(actions=actions, resources=["*"]))
        self.resource = cdk.CustomResource(
            self,
            "Resource",
            service_token=function.function_arn,
            resource_type="Custom::DeployTimeLookup",
            properties={"Kind": kind, **properties},
        )
        self.value = self.resource.get_att_string("Value")


def hosted_zone_id(
    scope: Construct, construct_id: str, zone_name: str, *, private: bool = False
) -> str:
    return DeployTimeLookup(
        scope,
        construct_id,
        kind="HostedZone",
        properties={"ZoneName": zone_name, "Private": str(private).lower()},
        actions=["route53:ListHostedZonesByName"],
    ).value


def sso_role_arn(scope: Construct, construct_id: str, permission_set: str) -> str:
    return DeployTimeLookup(
        scope,
        construct_id,
        kind="SsoRole",
        properties={"PermissionSet": permission_set},
        actions=["iam:ListRoles"],
    ).value
