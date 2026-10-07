import json
from pathlib import Path

import aws_cdk as cdk
import pytest

from cognitech_cdk.common.config import available_environments, load_environment
from cognitech_cdk.stacks.network_stack import NetworkStack

CDK_JSON = Path(__file__).resolve().parents[2] / "cdk.json"


@pytest.mark.parametrize("env_name", available_environments())
def test_environment_synthesizes_with_cdk_json_context(env_name, tmp_path):
    """Catches invalid feature flags and duplicate stack names before the CLI does."""
    context = json.loads(CDK_JSON.read_text(encoding="utf-8"))["context"]
    config = load_environment(env_name)

    app = cdk.App(context=context, outdir=str(tmp_path / env_name))
    for network in config.networks:
        NetworkStack(
            app,
            config.common.resource_name(network.name, "network"),
            common=config.common,
            network=network,
            env=cdk.Environment(account=config.account_id, region=config.region),
        )

    assembly = app.synth()
    assert assembly.stacks, f"'{env_name}' produced no stacks"
