#!/usr/bin/env python3
"""CDK app entrypoint.

The environment is a folder name under `deployments/`. It is resolved in this
order, first match wins:

1. `--context env=<name>` on the command line
2. the `ENV` environment variable
3. `DEFAULT_ENV` below

So these are equivalent:

    cdk deploy --context env=prod
    ENV=prod cdk deploy

Or set it once for the whole terminal:

    export ENV=prod
    cdk deploy

Add a stack by importing it and registering it at the bottom.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import aws_cdk as cdk  # noqa: E402

from cognitech_cdk.common.environment import (  # noqa: E402
    list_environments,
    load_environment,
)
from cognitech_cdk.stacks.backup_stack import BackupStack  # noqa: E402
from cognitech_cdk.stacks.certificates_stack import CertificatesStack  # noqa: E402
from cognitech_cdk.stacks.cognito_stack import CognitoStack  # noqa: E402
from cognitech_cdk.stacks.ecr_stack import EcrStack  # noqa: E402
from cognitech_cdk.stacks.ecs_stack import EcsStack  # noqa: E402
from cognitech_cdk.stacks.eks_stack import EksStack  # noqa: E402
from cognitech_cdk.stacks.events_stack import EventsStack  # noqa: E402
from cognitech_cdk.stacks.firehose_stack import FirehoseStack  # noqa: E402
from cognitech_cdk.stacks.iam_stack import IamStack  # noqa: E402
from cognitech_cdk.stacks.key_pairs_stack import KeyPairsStack  # noqa: E402
from cognitech_cdk.stacks.lambda_stack import LambdaStack  # noqa: E402
from cognitech_cdk.stacks.load_balancing_stack import LoadBalancingStack  # noqa: E402
from cognitech_cdk.stacks.network_stack import NetworkStack  # noqa: E402
from cognitech_cdk.stacks.opensearch_stack import OpenSearchStack  # noqa: E402
from cognitech_cdk.stacks.rds_stack import RdsStack  # noqa: E402
from cognitech_cdk.stacks.secrets_stack import SecretsStack  # noqa: E402
from cognitech_cdk.stacks.security_stack import SecurityStack  # noqa: E402
from cognitech_cdk.stacks.ssm_stack import SsmStack  # noqa: E402
from cognitech_cdk.stacks.storage_stack import StorageStack  # noqa: E402
from cognitech_cdk.stacks.transit_gateway_stack import TransitGatewayStack  # noqa: E402
from cognitech_cdk.stacks.waf_stack import WafStack  # noqa: E402

DEFAULT_ENV = "dev"
ENV_VAR = "ENV"

app = cdk.App()

env_name = app.node.try_get_context("env") or os.environ.get(ENV_VAR) or DEFAULT_ENV

known = list_environments()
if env_name not in known:
    raise SystemExit(
        f"Unknown environment '{env_name}'. Known: {', '.join(known) or 'none'}.\n"
        f"Set it with --context env=<name> or {ENV_VAR}=<name>."
    )

environment = load_environment(env_name)
aws_env = cdk.Environment(account=environment.account_id, region=environment.region)

def stack_name(suffix: str) -> str:
    return environment.common.name(environment.network.name, suffix)


def options(description: str) -> dict:
    return {"env": aws_env, "description": f"{description} for {env_name}"}


network = NetworkStack(
    app, stack_name("network"), environment=environment,
    **options("VPC, subnets and gateways"),
)
net = network.network

# Each stack below exists only when env.yaml has its section: CloudFormation
# rejects a template with no resources. Passing objects between stacks lets CDK
# create the exports/imports and the deployment order.
security_groups = {}
if environment.security_groups:
    security = SecurityStack(
        app, stack_name("security"), environment=environment, vpc=net.vpc,
        **options("Security groups"),
    )
    security_groups = security.security_groups.groups

roles, instance_profiles = {}, {}
if environment.iam:
    iam = IamStack(
        app, stack_name("iam"), environment=environment,
        **options("IAM roles and instance profiles"),
    )
    roles, instance_profiles = iam.iam.roles, iam.iam.instance_profiles

if environment.transit_gateway:
    TransitGatewayStack(
        app, stack_name("transit-gateway"), environment=environment,
        props=environment.transit_gateway, network=net,
        **options("Transit gateway, attachments and routes"),
    )

buckets = {}
if environment.storage:
    buckets = StorageStack(
        app, stack_name("storage"), environment=environment, props=environment.storage,
        roles=roles, **options("Private S3 buckets"),
    ).buckets

key_pairs = {}
if environment.key_pairs:
    key_pairs = KeyPairsStack(
        app, stack_name("key-pairs"), environment=environment, props=environment.key_pairs,
        **options("EC2 key pairs"),
    ).key_pairs

secrets = {}
if environment.secrets:
    secrets = SecretsStack(
        app, stack_name("secrets"), environment=environment, props=environment.secrets,
        **options("Secrets Manager secrets"),
    ).secrets

if environment.ssm:
    SsmStack(
        app, stack_name("ssm"), environment=environment, props=environment.ssm,
        secrets=secrets, buckets=buckets, **options("SSM parameters and documents"),
    )

certificates = {}
if environment.certificates:
    certificates = CertificatesStack(
        app, stack_name("certificates"), environment=environment,
        props=environment.certificates, **options("ACM certificates"),
    ).certificates

if environment.backups:
    BackupStack(
        app, stack_name("backup"), environment=environment, props=environment.backups,
        **options("AWS Backup vaults and plans"),
    )

load_balancers, target_groups = {}, {}
if environment.load_balancing:
    load_balancing = LoadBalancingStack(
        app, stack_name("load-balancing"), environment=environment,
        props=environment.load_balancing, network=net, security_groups=security_groups,
        certificates=certificates, buckets=buckets,
        **options("Load balancers, target groups and listeners"),
    )
    load_balancers, target_groups = load_balancing.load_balancers, load_balancing.target_groups

if environment.waf:
    WafStack(
        app, stack_name("waf"), environment=environment, props=environment.waf,
        load_balancers=load_balancers, **options("WAF IP sets, rule groups and web ACLs"),
    )

repositories = {}
if environment.ecr:
    repositories = EcrStack(
        app, stack_name("ecr"), environment=environment, props=environment.ecr,
        **options("ECR repositories"),
    ).repositories

# Access entries, node groups, add-ons, Karpenter and Helm apps are nested
# stacks inside the EKS stack, so the whole cluster deploys as one unit.
if environment.eks:
    EksStack(
        app, stack_name("eks"), environment=environment, props=environment.eks,
        network=net, security_groups=security_groups, roles=roles, key_pairs=key_pairs,
        addons=environment.eks_addons, certificates=certificates,
        **options("EKS clusters with nested access, node groups, add-ons and apps"),
    )
elif environment.eks_addons:
    raise SystemExit("eks_addons needs an eks section: add-ons deploy inside the EKS stack.")

if environment.ecs:
    EcsStack(
        app, stack_name("ecs"), environment=environment, props=environment.ecs,
        network=net, security_groups=security_groups, roles=roles,
        instance_profiles=instance_profiles, target_groups=target_groups,
        load_balancers=load_balancers, repositories=repositories, key_pairs=key_pairs,
        secrets=secrets, **options("ECS clusters and services"),
    )

if environment.rds:
    RdsStack(
        app, stack_name("rds"), environment=environment, props=environment.rds,
        network=net, security_groups=security_groups, secrets=secrets,
        **options("RDS instances"),
    )

domains = {}
if environment.opensearch:
    domains = OpenSearchStack(
        app, stack_name("opensearch"), environment=environment, props=environment.opensearch,
        network=net, security_groups=security_groups, secrets=secrets,
        certificates=certificates, **options("OpenSearch domains"),
    ).domains

if environment.firehose:
    FirehoseStack(
        app, stack_name("firehose"), environment=environment, props=environment.firehose,
        buckets=buckets, domains=domains, roles=roles, network=net,
        security_groups=security_groups, **options("Firehose delivery streams"),
    )

functions = {}
if environment.lambdas:
    functions = LambdaStack(
        app, stack_name("lambda"), environment=environment, props=environment.lambdas,
        network=net, security_groups=security_groups, roles=roles, buckets=buckets,
        **options("Lambda functions and invocations"),
    ).functions

if environment.events:
    EventsStack(
        app, stack_name("events"), environment=environment, props=environment.events,
        functions=functions, **options("EventBridge buses and rules"),
    )

if environment.cognito:
    CognitoStack(
        app, stack_name("cognito"), environment=environment, props=environment.cognito,
        certificates=certificates, functions=functions, **options("Cognito user pools"),
    )

app.synth()
