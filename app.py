#!/usr/bin/env python3
"""CDK app entrypoint. Select the environment with `--context env=<name>`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import aws_cdk as cdk  # noqa: E402

from cognitech_cdk.common.config import load_environment  # noqa: E402
from cognitech_cdk.stacks.network_stack import NetworkStack  # noqa: E402

app = cdk.App()

env_name = app.node.try_get_context("env") or "uat"
config = load_environment(env_name)
aws_env = cdk.Environment(account=config.account_id, region=config.region)

for network in config.networks:
    NetworkStack(
        app,
        config.common.resource_name(network.name, "network"),
        common=config.common,
        network=network,
        env=aws_env,
        description=f"Network baseline for {network.name} ({env_name})",
    )

app.synth()
