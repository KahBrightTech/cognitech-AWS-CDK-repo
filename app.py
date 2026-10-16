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
from cognitech_cdk.stacks.iam_stack import IamStack  # noqa: E402
from cognitech_cdk.stacks.network_stack import NetworkStack  # noqa: E402
from cognitech_cdk.stacks.security_stack import SecurityStack  # noqa: E402

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

network = NetworkStack(
    app,
    environment.common.name(environment.network.name, "network"),
    environment=environment,
    env=aws_env,
    description=f"VPC, subnets and gateways for {env_name}",
)

# Only built where the environment declares groups: CloudFormation rejects a
# template with no resources, so an empty security_groups list means no stack.
if environment.security_groups:
    SecurityStack(
        app,
        environment.common.name(environment.network.name, "security"),
        environment=environment,
        vpc=network.network.vpc,
        env=aws_env,
        description=f"Security groups for {env_name}",
    )

if environment.iam.roles:
    IamStack(
        app,
        environment.common.name(environment.network.name, "iam"),
        environment=environment,
        env=aws_env,
        description=f"IAM roles and instance profiles for {env_name}",
    )

app.synth()
