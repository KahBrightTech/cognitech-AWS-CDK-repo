"""Loads legacy or account-grouped deployments into typed objects.

Account-grouped environments inherit `deployments/<account>/base.yaml`.
Environment mappings override defaults recursively; lists and scalars replace
them. Repository-relative references keep resolving from the project root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from cognitech_cdk.common import schema
from cognitech_cdk.common.config import Common, merge_config
from cognitech_cdk.common.documents import placeholders, render
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
BASE_FILE = "base.yaml"
RESOURCE_TEXT_FIELDS = frozenset({
    "name", "description", "secret_name", "secret_description", "role_name",
    "bucket_name", "function_name", "cluster_name", "group_name", "key_pair", "role",
})
DOCUMENT_FIELDS = frozenset({
    "common", "tags", "values", "variables", "policy", "policy_variables",
    "configuration_values", "node_class_manifest", "node_pool_manifest",
})


def _render_resource_fields(
    raw: Mapping[str, Any], values: Mapping[str, str], label: str,
) -> dict[str, Any]:
    result = {}
    for key, value in raw.items():
        where = f"{label}.{key}"
        if key in DOCUMENT_FIELDS:
            result[key] = value
        elif isinstance(value, Mapping):
            result[key] = _render_resource_fields(value, values, where)
        elif isinstance(value, list):
            result[key] = [
                _render_resource_fields(item, values, f"{where}[{index}]")
                if isinstance(item, Mapping) else item
                for index, item in enumerate(value)
            ]
        elif key in RESOURCE_TEXT_FIELDS and isinstance(value, str) and placeholders(value):
            result[key] = render(value, values, where)
        else:
            result[key] = value
    return result


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


def resolve_environment(env_name: str, root: Path = DEPLOYMENTS_DIR) -> str:
    """Resolve a qualified name or an unambiguous legacy short name."""
    parts = env_name.split("/")
    if len(parts) not in (1, 2):
        raise ValueError("Environment must be <environment> or <account>/<environment>.")
    for part in parts:
        schema.name(part, "Environment path component")
    known = list_environments(root)
    matches = (
        [name for name in known if name == env_name]
        if len(parts) == 2
        else [name for name in known if name.split("/")[-1] == env_name]
    )
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous environment '{env_name}'. Choose an account/environment: {', '.join(matches)}."
        )
    if not matches:
        raise FileNotFoundError(
            f"Unknown environment '{env_name}'. Expected {root / env_name / ENV_FILE}. "
            f"Known environments: {', '.join(known) or 'none'}."
        )
    return matches[0]


def _read_config(path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML in {path}: {error}") from error
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{path} must contain a mapping with string keys.")
    return raw


def load_environment_config(env_name: str, root: Path = DEPLOYMENTS_DIR) -> Mapping[str, Any]:
    """Load merged deployment YAML and resolve its built-in placeholders."""
    selected = resolve_environment(env_name, root)
    path = root / selected / ENV_FILE
    raw = _read_config(path)
    if "/" in selected:
        defaults = path.parent.parent / BASE_FILE
        if defaults.is_file():
            raw = merge_config(_read_config(defaults), raw)
    missing = [key for key in ("account_id", "region", "network") if raw.get(key) in (None, "")]
    if missing:
        raise ValueError(f"{path}: missing required fields after merging: {', '.join(missing)}.")
    common = Common.from_dict(raw.get("common") or {}, path.parent.name)
    values = {
        "account_id": str(raw["account_id"]),
        "account_name": common.account_name,
        "account_name_abr": common.account_name_abr or common.account_name,
        "region": str(raw["region"]),
        "region_prefix": common.region_prefix,
        "environment": path.parent.name,
    }
    # Preserve placeholders belonging to policy, Helm and manifest renderers.
    raw = render(raw, {
        **{key: f"[[{key}]]" for key in placeholders(raw) - values.keys()},
        **values,
    }, str(path))
    raw = _render_resource_fields(raw, values, str(path))
    return raw


def load_environment(env_name: str, root: Path = DEPLOYMENTS_DIR) -> Environment:
    raw = load_environment_config(env_name, root)
    path = root / resolve_environment(env_name, root) / ENV_FILE
    # Repository-relative files (policies, rule files, code) resolve from the
    # project root alongside deployments/, not the environment folder.
    base_dir = root.parent
    lambdas = (
        {key: raw[key] for key in ("lambdas", "lambda_invocations") if key in raw} or None
    )

    return Environment(
        name=path.parent.name,
        account_id=str(raw["account_id"]),
        region=str(raw["region"]),
        common=Common.from_dict(raw.get("common") or {}, path.parent.name),
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
        eks=EksProps.from_dict(
            raw.get("eks"), base_dir=base_dir, principal_aliases=raw.get("eks_access_principals"),
        ),
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
    """Canonical legacy and account/environment names under `deployments/`."""
    if not root.is_dir():
        return []
    return sorted(
        path.parent.relative_to(root).as_posix()
        for pattern in (f"*/{ENV_FILE}", f"*/*/{ENV_FILE}")
        for path in root.glob(pattern)
        if path.is_file()
    )
