"""Config-driven ECR repositories, mirroring the Terraform `ECR` module.

    ecr_repositories:
      - name: backend
        image_tag_mutability: IMMUTABLE      # MUTABLE (default) | IMMUTABLE
        scan_on_push: true                   # default true
        encryption_type: KMS                 # AES256 (default) | KMS
        kms_key_arn: arn:aws:kms:...         # required for KMS
        removal_policy: retain               # retain (default) | destroy
        force_delete: false                  # EmptyOnDelete; only meaningful with destroy
        lifecycle_policy: policies/ecr-lifecycle.json   # file path or inline mapping
        repository_policy:                   # file path or inline mapping
          Version: "2012-10-17"
          Statement: [...]
        replication_configuration:           # registry-wide; at most one repository
          rules:
            - destinations: [{region: us-west-2, registry_id: "111122223333"}]
              repository_filter: {filter: int-production, filter_type: PREFIX_MATCH}

Policy documents may use the Terraform placeholders `[[account_number]]`,
`[[account_name_abr]]`, `[[region]]`, `[[admin_role]]` and `[[network_role]]`.
The SSO role ARNs are resolved at deploy time, never during synth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.common.documents import (
    json_compatible,
    load_document,
    placeholders,
    render,
)
from cognitech_cdk.constructs.lookups import sso_role_arn

MUTABILITY = ("MUTABLE", "IMMUTABLE")
ENCRYPTION = ("AES256", "KMS")
REMOVAL = ("retain", "destroy")
FILTER_TYPES = ("PREFIX_MATCH",)
PLACEHOLDERS = ("account_number", "account_name_abr", "region", "admin_role", "network_role")
REPOSITORY_NAME = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*")
KMS_ARN = re.compile(r"arn:[a-z0-9-]+:kms:[a-z0-9-]+:[0-9]{12}:(?:key|alias)/[A-Za-z0-9/_-]+")
ACCOUNT_ID = re.compile(r"[0-9]{12}")
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]")


def logical_id(name: str) -> str:
    """`backend-api` -> `BackendApi`, keeping logical ids readable and alphanumeric."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name) if part)


def _check_placeholders(document: Any, label: str) -> None:
    unknown = sorted(placeholders(document) - set(PLACEHOLDERS))
    if unknown:
        raise ValueError(
            f"{label}: unknown placeholders: {', '.join(unknown)}. "
            f"Supported: {', '.join(PLACEHOLDERS)}."
        )


def _statements(document: Mapping[str, Any]) -> list[Any]:
    value = document.get("Statement")
    return [value] if isinstance(value, Mapping) else value


def _is_public(principal: Any) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, Mapping):
        return any(
            value == "*" or (isinstance(value, list) and "*" in value)
            for value in principal.values()
        )
    return False


def _repository_policy(raw: Any, label: str, *, base_dir: Path | None) -> dict[str, Any]:
    document = schema.mapping(
        load_document(raw, label, base_dir=base_dir), label, {"Version", "Id", "Statement"}
    )
    if document.get("Version", "2012-10-17") not in ("2012-10-17", "2008-10-17"):
        raise ValueError(f"{label}: Version must be a quoted IAM version string.")
    statements = document.get("Statement")
    if not isinstance(statements, (Mapping, list)) or not statements:
        raise ValueError(f"{label}: Statement must be a statement or a nonempty list.")
    for index, raw_statement in enumerate(_statements(document)):
        where = f"{label}, statement {index + 1}"
        statement = schema.mapping(raw_statement, where, {
            "Sid", "Effect", "Action", "NotAction", "Principal", "NotPrincipal",
            "Condition", "Resource",
        })
        if statement.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f"{where}: Effect must be Allow or Deny.")
        if ("Action" in statement) == ("NotAction" in statement):
            raise ValueError(f"{where}: provide exactly one of Action or NotAction.")
        schema.strings(
            statement.get("Action", statement.get("NotAction")), f"{where}: Action",
            allow_empty=False,
        )
        if ("Principal" in statement) == ("NotPrincipal" in statement):
            raise ValueError(f"{where}: provide exactly one of Principal or NotPrincipal.")
        if statement["Effect"] == "Allow" and (
            "NotPrincipal" in statement or _is_public(statement.get("Principal"))
        ) and not statement.get("Condition"):
            raise ValueError(
                f"{where}: refusing a public repository policy; a wildcard principal "
                "needs a restrictive Condition."
            )
        if "Condition" in statement and (
            not isinstance(statement["Condition"], Mapping) or not statement["Condition"]
        ):
            raise ValueError(f"{where}: Condition must be a nonempty mapping.")
    _check_placeholders(document, label)
    return json_compatible(document, label)


def _lifecycle_policy(raw: Any, label: str, *, base_dir: Path | None) -> dict[str, Any]:
    document = schema.mapping(load_document(raw, label, base_dir=base_dir), label, {"rules"})
    rules = document.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"{label}: rules must be a nonempty list.")
    priorities: set[int] = set()
    for index, rule in enumerate(rules):
        where = f"{label}, rule {index + 1}"
        rule = schema.mapping(
            rule, where, {"rulePriority", "description", "selection", "action"},
            required=("rulePriority", "selection", "action"),
        )
        priority = schema.integer(rule["rulePriority"], f"{where}: rulePriority", minimum=1)
        if priority in priorities:
            raise ValueError(f"{where}: duplicate rulePriority {priority}.")
        priorities.add(priority)
        if not isinstance(rule["selection"], Mapping) or "tagStatus" not in rule["selection"]:
            raise ValueError(f"{where}: selection must be a mapping with tagStatus.")
        if not isinstance(rule["action"], Mapping) or rule["action"].get("type") not in (
            "expire", "transition"
        ):
            raise ValueError(f"{where}: action.type must be expire or transition.")
    _check_placeholders(document, label)
    return json_compatible(document, label)


@dataclass(frozen=True)
class ReplicationDestination:
    region: str
    registry_id: str


@dataclass(frozen=True)
class ReplicationRule:
    destinations: tuple[ReplicationDestination, ...]
    filter: str | None = None
    filter_type: str = "PREFIX_MATCH"


@dataclass(frozen=True)
class EcrRepositoryProps:
    name: str
    image_tag_mutability: str = "MUTABLE"
    scan_on_push: bool = True
    encryption_type: str = "AES256"
    kms_key_arn: str | None = None
    force_delete: bool = False
    removal_policy: str = "retain"
    lifecycle_policy: Mapping[str, Any] | None = None
    repository_policy: Mapping[str, Any] | None = None
    replication_rules: tuple[ReplicationRule, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EcrRepositoryProps:
        data = schema.mapping(raw, "ECR repository", {
            "name", "image_tag_mutability", "scan_on_push", "encryption_type",
            "kms_key_arn", "force_delete", "removal_policy", "lifecycle_policy",
            "repository_policy", "replication_configuration",
        }, required=("name",))
        name = schema.name(data["name"], "ECR repository name")
        label = f"ECR repository '{name}'"
        if not REPOSITORY_NAME.fullmatch(name):
            raise ValueError(
                f"{label}: name must be lowercase letters, digits, '.', '_' or '-'."
            )
        mutability = schema.string(
            data.get("image_tag_mutability"), f"{label}: image_tag_mutability", default="MUTABLE"
        )
        if mutability not in MUTABILITY:
            raise ValueError(f"{label}: image_tag_mutability must be MUTABLE or IMMUTABLE.")
        encryption = schema.string(
            data.get("encryption_type"), f"{label}: encryption_type", default="AES256"
        )
        if encryption not in ENCRYPTION:
            raise ValueError(f"{label}: encryption_type must be AES256 or KMS.")
        kms_key_arn = schema.string(data.get("kms_key_arn"), f"{label}: kms_key_arn")
        if encryption == "KMS" and kms_key_arn is None:
            raise ValueError(f"{label}: kms_key_arn must be provided when encryption_type is KMS.")
        if encryption != "KMS" and kms_key_arn is not None:
            raise ValueError(f"{label}: kms_key_arn requires encryption_type KMS.")
        if kms_key_arn is not None and not KMS_ARN.fullmatch(kms_key_arn):
            raise ValueError(f"{label}: kms_key_arn must be a KMS key or alias ARN.")
        removal = schema.string(
            data.get("removal_policy"), f"{label}: removal_policy", default="retain"
        )
        if removal not in REMOVAL:
            raise ValueError(f"{label}: removal_policy must be retain or destroy.")
        lifecycle = (
            _lifecycle_policy(
                data["lifecycle_policy"], f"{label}, lifecycle_policy", base_dir=base_dir
            )
            if data.get("lifecycle_policy") is not None else None
        )
        policy = (
            _repository_policy(
                data["repository_policy"], f"{label}, repository_policy", base_dir=base_dir
            )
            if data.get("repository_policy") is not None else None
        )
        return cls(
            name=name,
            image_tag_mutability=mutability,
            scan_on_push=schema.boolean(
                data.get("scan_on_push"), f"{label}: scan_on_push", default=True
            ),
            encryption_type=encryption,
            kms_key_arn=kms_key_arn,
            force_delete=schema.boolean(data.get("force_delete"), f"{label}: force_delete"),
            removal_policy=removal,
            lifecycle_policy=lifecycle,
            repository_policy=policy,
            replication_rules=_replication(data.get("replication_configuration"), label),
        )


def _replication(raw: Any, label: str) -> tuple[ReplicationRule, ...]:
    if raw is None:
        return ()
    label = f"{label}, replication_configuration"
    data = schema.mapping(raw, label, {"rules"}, required=("rules",))
    if not isinstance(data["rules"], list) or not data["rules"]:
        raise ValueError(f"{label}: rules must be a nonempty list.")
    rules = []
    for index, raw_rule in enumerate(data["rules"]):
        where = f"{label}, rule {index + 1}"
        rule = schema.mapping(
            raw_rule, where, {"destinations", "repository_filter"}, required=("destinations",)
        )
        if not isinstance(rule["destinations"], list) or not rule["destinations"]:
            raise ValueError(f"{where}: destinations must be a nonempty list.")
        destinations = []
        for raw_destination in rule["destinations"]:
            destination = schema.mapping(
                raw_destination, f"{where}: destination", {"region", "registry_id"},
                required=("region", "registry_id"),
            )
            region = str(destination["region"])
            registry = str(destination["registry_id"])
            if not REGION.fullmatch(region) or not ACCOUNT_ID.fullmatch(registry):
                raise ValueError(
                    f"{where}: destinations need an AWS region and a 12-digit registry_id."
                )
            destinations.append(ReplicationDestination(region, registry))
        repository_filter = None
        filter_type = "PREFIX_MATCH"
        if rule.get("repository_filter") is not None:
            filter_data = schema.mapping(
                rule["repository_filter"], f"{where}: repository_filter",
                {"filter", "filter_type"}, required=("filter",),
            )
            repository_filter = schema.required_string(filter_data["filter"], f"{where}: filter")
            filter_type = schema.string(
                filter_data.get("filter_type"), f"{where}: filter_type", default="PREFIX_MATCH"
            )
            if filter_type not in FILTER_TYPES:
                raise ValueError(f"{where}: filter_type must be PREFIX_MATCH.")
        rules.append(ReplicationRule(tuple(destinations), repository_filter, filter_type))
    return tuple(rules)


@dataclass(frozen=True)
class EcrProps:
    repositories: tuple[EcrRepositoryProps, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        replicated = [repo.name for repo in self.repositories if repo.replication_rules]
        if len(replicated) > 1:
            raise ValueError(
                "ecr_repositories: replication_configuration is registry-wide; set it on "
                f"one repository only (found {', '.join(replicated)})."
            )

    def __bool__(self) -> bool:
        return bool(self.repositories)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Path | None = None) -> EcrProps:
        return cls(
            repositories=schema.items(
                raw, "ecr_repositories",
                lambda item: EcrRepositoryProps.from_dict(item, base_dir=base_dir),
            )
        )


class EcrRepositories(Construct):
    """One `AWS::ECR::Repository` per configured entry, plus optional replication."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        props: EcrProps,
        name: Callable[..., str],
        account_name_abr: str = "",
    ) -> None:
        super().__init__(scope, construct_id)
        self.resources: dict[str, ecr.CfnRepository] = {}
        self.repositories: dict[str, ecr.IRepository] = {}
        self._lookups: dict[str, str] = {}
        self._account_name_abr = account_name_abr

        for repo in props.repositories:
            full_name = name(repo.name)
            if not 2 <= len(full_name) <= 256 or not REPOSITORY_NAME.fullmatch(full_name):
                raise ValueError(
                    f"ECR repository '{repo.name}': final name '{full_name}' must be "
                    "2-256 lowercase characters."
                )
            label = f"ECR repository '{repo.name}'"
            resource = ecr.CfnRepository(
                self,
                logical_id(repo.name),
                repository_name=full_name,
                image_tag_mutability=repo.image_tag_mutability,
                image_scanning_configuration=ecr.CfnRepository.ImageScanningConfigurationProperty(
                    scan_on_push=repo.scan_on_push
                ),
                encryption_configuration=ecr.CfnRepository.EncryptionConfigurationProperty(
                    encryption_type=repo.encryption_type, kms_key=repo.kms_key_arn
                ),
                empty_on_delete=repo.force_delete,
                lifecycle_policy=(
                    ecr.CfnRepository.LifecyclePolicyProperty(
                        lifecycle_policy_text=cdk.Stack.of(self).to_json_string(
                            self._render(repo.lifecycle_policy, f"{label}, lifecycle_policy")
                        )
                    )
                    if repo.lifecycle_policy is not None else None
                ),
                repository_policy_text=(
                    self._render(repo.repository_policy, f"{label}, repository_policy")
                    if repo.repository_policy is not None else None
                ),
            )
            resource.apply_removal_policy(
                cdk.RemovalPolicy.DESTROY if repo.removal_policy == "destroy"
                else cdk.RemovalPolicy.RETAIN
            )
            cdk.Tags.of(resource).add("Name", full_name)
            self.resources[repo.name] = resource
            self.repositories[repo.name] = ecr.Repository.from_repository_attributes(
                self,
                f"{logical_id(repo.name)}Reference",
                repository_arn=resource.attr_arn,
                repository_name=resource.ref,
            )
            if repo.replication_rules:
                self.replication = ecr.CfnReplicationConfiguration(
                    self,
                    "Replication",
                    replication_configuration=ecr.CfnReplicationConfiguration.ReplicationConfigurationProperty(
                        rules=[
                            ecr.CfnReplicationConfiguration.ReplicationRuleProperty(
                                destinations=[
                                    ecr.CfnReplicationConfiguration.ReplicationDestinationProperty(
                                        region=destination.region,
                                        registry_id=destination.registry_id,
                                    )
                                    for destination in rule.destinations
                                ],
                                repository_filters=(
                                    [
                                        ecr.CfnReplicationConfiguration.RepositoryFilterProperty(
                                            filter=rule.filter, filter_type=rule.filter_type
                                        )
                                    ]
                                    if rule.filter is not None else None
                                ),
                            )
                            for rule in repo.replication_rules
                        ]
                    ),
                )

    def _render(self, document: Mapping[str, Any], label: str) -> Any:
        used = placeholders(document)
        values: dict[str, str] = {}
        for key in used:
            if key == "account_number":
                values[key] = cdk.Aws.ACCOUNT_ID
            elif key == "region":
                values[key] = cdk.Aws.REGION
            elif key == "account_name_abr":
                if not self._account_name_abr:
                    raise ValueError(f"{label}: [[account_name_abr]] needs common.account_name_abr.")
                values[key] = self._account_name_abr
            else:
                permission_set = {
                    "admin_role": "AdministratorAccess",
                    "network_role": "NetworkAdministrator",
                }[key]
                if key not in self._lookups:
                    self._lookups[key] = sso_role_arn(
                        self, f"{logical_id(key)}Lookup", permission_set
                    )
                values[key] = self._lookups[key]
        return render(document, values, label)
