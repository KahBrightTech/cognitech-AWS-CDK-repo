"""Loads `deployments/<env>/env.yaml` into typed objects.

Each stack gets its own props block in the YAML, so adding a stack means adding
a key here and a dataclass next to its construct.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from cognitech_cdk.common.config import Common
from cognitech_cdk.constructs.backup import BackupProps
from cognitech_cdk.constructs.certificates import CertificatesProps
from cognitech_cdk.constructs.cognito import CognitoProps
from cognitech_cdk.constructs.ecr import EcrProps
from cognitech_cdk.constructs.ecs import EcsProps
from cognitech_cdk.constructs.eks import EksProps
from cognitech_cdk.constructs.eks_addons import EksAddonsProps
from cognitech_cdk.constructs.events import EventsProps
from cognitech_cdk.constructs.firehose import FirehoseProps
from cognitech_cdk.constructs.iam import IamProps
from cognitech_cdk.constructs.key_pairs import KeyPairsProps
from cognitech_cdk.constructs.lambdas import LambdaProps
from cognitech_cdk.constructs.load_balancing import LoadBalancingProps
from cognitech_cdk.constructs.network import NetworkProps
from cognitech_cdk.constructs.opensearch import OpenSearchProps
from cognitech_cdk.constructs.rds import RdsProps
from cognitech_cdk.constructs.secrets import SecretsProps
from cognitech_cdk.constructs.security import SecurityGroupProps
from cognitech_cdk.constructs.ssm import SsmProps
from cognitech_cdk.constructs.storage import StorageProps
from cognitech_cdk.constructs.transit_gateway import TransitGatewayProps
from cognitech_cdk.constructs.waf import WafProps

DEPLOYMENTS_DIR = Path(__file__).resolve().parents[3] / "deployments"
ENV_FILE = "env.yaml"


@dataclass(frozen=True)
class Environment:
    name: str
    account_id: str
    region: str
    common: Common
    network: NetworkProps
    security_groups: tuple[SecurityGroupProps, ...] = ()
    iam: IamProps = IamProps()
    transit_gateway: TransitGatewayProps = TransitGatewayProps()
    storage: StorageProps = StorageProps()
    key_pairs: KeyPairsProps = KeyPairsProps()
    secrets: SecretsProps = SecretsProps()
    ssm: SsmProps = SsmProps()
    certificates: CertificatesProps = CertificatesProps()
    backups: BackupProps = BackupProps()
    load_balancing: LoadBalancingProps = LoadBalancingProps()
    waf: WafProps = WafProps()
    eks: EksProps = EksProps()
    eks_addons: EksAddonsProps = EksAddonsProps()
    ecr: EcrProps = EcrProps()
    ecs: EcsProps = EcsProps()
    rds: RdsProps = RdsProps()
    opensearch: OpenSearchProps = OpenSearchProps()
    firehose: FirehoseProps = FirehoseProps()
    lambdas: LambdaProps = LambdaProps()
    events: EventsProps = EventsProps()
    cognito: CognitoProps = CognitoProps()

    @property
    def cdk_env(self) -> Mapping[str, str]:
        return {"account": self.account_id, "region": self.region}


def load_environment(env_name: str, root: Path = DEPLOYMENTS_DIR) -> Environment:
    path = root / env_name / ENV_FILE
    if not path.is_file():
        known = ", ".join(list_environments(root)) or "none"
        raise FileNotFoundError(
            f"No config for '{env_name}'. Expected {path}. Known environments: {known}."
        )

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # Repository-relative files (policies, rule files, code) resolve from the
    # project root alongside deployments/, not the environment folder.
    base_dir = root.parent
    lambdas = (
        {key: raw[key] for key in ("lambdas", "lambda_invocations") if key in raw} or None
    )

    return Environment(
        name=env_name,
        account_id=str(raw["account_id"]),
        region=str(raw["region"]),
        common=Common.from_dict(raw.get("common") or {}, env_name),
        network=NetworkProps.from_dict(raw["network"]),
        security_groups=SecurityGroupProps.load_all(raw.get("security_groups")),
        iam=IamProps.from_dict(raw.get("iam"), base_dir=base_dir),
        transit_gateway=TransitGatewayProps.from_dict(raw.get("transit_gateway"), base_dir=base_dir),
        storage=StorageProps.from_dict(raw.get("s3_buckets"), base_dir=base_dir),
        key_pairs=KeyPairsProps.from_dict(raw.get("key_pairs"), base_dir=base_dir),
        secrets=SecretsProps.from_dict(raw.get("secrets"), base_dir=base_dir),
        ssm=SsmProps.from_dict(raw.get("ssm"), base_dir=base_dir),
        certificates=CertificatesProps.from_dict(raw.get("certificates"), base_dir=base_dir),
        backups=BackupProps.from_dict(raw.get("backups"), base_dir=base_dir),
        load_balancing=LoadBalancingProps.from_dict(raw.get("load_balancing"), base_dir=base_dir),
        waf=WafProps.from_dict(raw.get("waf"), base_dir=base_dir),
        eks=EksProps.from_dict(raw.get("eks"), base_dir=base_dir),
        eks_addons=EksAddonsProps.from_dict(raw.get("eks_addons"), base_dir=base_dir),
        ecr=EcrProps.from_dict(raw.get("ecr_repositories"), base_dir=base_dir),
        ecs=EcsProps.from_dict(raw.get("ecs_clusters"), base_dir=base_dir),
        rds=RdsProps.from_dict(raw.get("rds_instances"), base_dir=base_dir),
        opensearch=OpenSearchProps.from_dict(raw.get("opensearch_domains"), base_dir=base_dir),
        firehose=FirehoseProps.from_dict(raw.get("firehose_streams"), base_dir=base_dir),
        lambdas=LambdaProps.from_dict(lambdas, base_dir=base_dir),
        events=EventsProps.from_dict(raw.get("events"), base_dir=base_dir),
        cognito=CognitoProps.from_dict(raw.get("cognito"), base_dir=base_dir),
    )


def list_environments(root: Path = DEPLOYMENTS_DIR) -> list[str]:
    """Environment names found under `deployments/`."""
    if not root.is_dir():
        return []
    return sorted(path.parent.name for path in root.glob(f"*/{ENV_FILE}"))
