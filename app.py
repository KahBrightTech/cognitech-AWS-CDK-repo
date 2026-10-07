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
from cognitech_cdk.stacks.network_stack import NetworkStack  # noqa: E402

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

NetworkStack(
    app,
    environment.common.name(environment.network.name, "network"),
    environment=environment,
    env=aws_env,
    description=f"VPC, subnets and gateways for {env_name}",
)

app.synth()
