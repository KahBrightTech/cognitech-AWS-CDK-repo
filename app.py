#!/usr/bin/env python3
"""CDK app. Pick the environment with `--context env=<name>`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import aws_cdk as cdk  # noqa: E402

from cognitech_cdk.network_stack import NetworkStack  # noqa: E402
from cognitech_cdk.settings import load_settings  # noqa: E402

app = cdk.App()

env = app.node.try_get_context("env") or "uat"
settings = load_settings(env)

NetworkStack(
    app,
    f"{settings.name_prefix}-network",
    settings=settings,
    env=cdk.Environment(account=settings.account_id, region=settings.region),
    description=f"VPC and subnets for {env}",
)

app.synth()
