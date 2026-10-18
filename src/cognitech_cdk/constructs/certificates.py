"""Public ACM certificates with Route 53 DNS validation (Terraform `ACM-Public-Certs`).

Terraform looks the zone up with `data.aws_route53_zone`; here the zone id is
either given or resolved at deploy time with `hosted_zone_id(...)`, so synth
needs no AWS credentials. CloudFormation creates the validation records itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_kms as kms
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common import schema
from cognitech_cdk.constructs.lookups import hosted_zone_id
from cognitech_cdk.constructs.secrets import removal_policy

VALIDATION_METHODS = ("DNS", "EMAIL")
DOMAIN = re.compile(r"(\*\.)?([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\.?")
ZONE_ID = re.compile(r"Z[A-Z0-9]{1,32}")
SECRET_NAME = re.compile(r"[A-Za-z0-9/_+=.@-]{1,512}")
KMS_ARN = re.compile(r"arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:(key|alias)/.+")


def _domain(value: Any, label: str) -> str:
    text = schema.required_string(value, label)
    if not DOMAIN.fullmatch(text):
        raise ValueError(f"{label} must be a domain name such as app.example.com or *.example.com.")
    return text


@dataclass(frozen=True)
class CertificateSecretProps:
    """Optional non-secret metadata published for consumers (ACM keys are not exportable)."""

    name: str | None = None
    description: str = "Metadata for ACM public certificate"
    kms_key_arn: str | None = None
    recovery_window_in_days: int = 7
    removal_policy: cdk.RemovalPolicy = cdk.RemovalPolicy.RETAIN

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "CertificateSecretProps | None":
        if raw is None:
            return None
        raw = schema.mapping(raw, label, {
            "enabled", "name", "description", "kms_key_id", "recovery_window_in_days", "removal_policy",
        })
        if not schema.boolean(raw.get("enabled"), f"{label}.enabled"):
            if set(raw) - {"enabled"}:
                raise ValueError(f"{label}: settings require enabled: true.")
            return None
        name = schema.string(raw.get("name"), f"{label}.name")
        if name is not None and not SECRET_NAME.fullmatch(name):
            raise ValueError(f"{label}.name may use letters, digits and /_+=.@- only.")
        kms_key_arn = schema.string(raw.get("kms_key_id"), f"{label}.kms_key_id")
        if kms_key_arn is not None and not KMS_ARN.fullmatch(kms_key_arn):
            raise ValueError(f"{label}.kms_key_id must be a KMS key or alias ARN.")
        days = schema.integer(raw.get("recovery_window_in_days"), f"{label}.recovery_window_in_days",
                              default=7, minimum=0, maximum=30)
        if 0 < days < 7:
            raise ValueError(f"{label}.recovery_window_in_days must be 0 or between 7 and 30.")
        return cls(
            name=name,
            description=schema.string(raw.get("description"), f"{label}.description",
                                      default=cls.description),
            kms_key_arn=kms_key_arn,
            recovery_window_in_days=days,
            removal_policy=removal_policy(raw.get("removal_policy"), f"{label}.removal_policy"),
        )


@dataclass(frozen=True)
class CertificateProps:
    name: str
    domain_name: str
    validation_method: str = "DNS"
    zone_name: str | None = None
    hosted_zone_id: str | None = None
    subject_alternative_names: tuple[str, ...] = ()
    secrets_manager: CertificateSecretProps | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "CertificateProps":
        raw = schema.mapping(raw, "certificates item", {
            "name", "domain_name", "validation_method", "zone_name", "hosted_zone_id",
            "subject_alternative_names", "secrets_manager",
        }, required=("name",))
        name = schema.name(raw["name"], "certificates item name")
        label = f"certificates '{name}'"
        method = raw.get("validation_method", "DNS")
        if method not in VALIDATION_METHODS:
            raise ValueError(f"{label}.validation_method must be one of: {', '.join(VALIDATION_METHODS)}.")
        zone_name = _domain(raw["zone_name"], f"{label}.zone_name") if raw.get("zone_name") is not None else None
        zone_id = schema.string(raw.get("hosted_zone_id"), f"{label}.hosted_zone_id")
        if zone_id is not None and not ZONE_ID.fullmatch(zone_id):
            raise ValueError(f"{label}.hosted_zone_id must look like Z0123456789ABC.")
        if method == "DNS" and zone_name is None:
            raise ValueError(f"{label}: DNS validation requires zone_name.")
        if method == "EMAIL" and zone_id is not None:
            raise ValueError(f"{label}: hosted_zone_id is only used with DNS validation.")
        return cls(
            name=name,
            domain_name=_domain(raw.get("domain_name"), f"{label}.domain_name"),
            validation_method=method,
            zone_name=zone_name.rstrip(".") if zone_name else None,
            hosted_zone_id=zone_id,
            subject_alternative_names=tuple(
                _domain(item, f"{label}.subject_alternative_names")
                for item in schema.strings(raw.get("subject_alternative_names"), f"{label}.subject_alternative_names")
            ),
            secrets_manager=CertificateSecretProps.from_dict(raw.get("secrets_manager"), f"{label}.secrets_manager"),
        )


@dataclass(frozen=True)
class CertificatesProps:
    certificates: tuple[CertificateProps, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.certificates)

    @classmethod
    def from_dict(cls, raw: Any, *, base_dir: Any = None) -> "CertificatesProps":
        return cls(certificates=schema.items(raw, "certificates", CertificateProps.from_dict))


class Certificates(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        props: CertificatesProps,
        naming: Callable[..., str],
    ) -> None:
        super().__init__(scope, construct_id)
        self.certificates: dict[str, acm.Certificate] = {}
        self.secrets: dict[str, secretsmanager.Secret] = {}
        zone_ids: dict[str, str] = {}

        for item in props.certificates:
            full_name = naming(item.name)
            if item.validation_method == "DNS":
                assert item.zone_name is not None
                zone_id = item.hosted_zone_id
                if zone_id is None:
                    if item.zone_name not in zone_ids:
                        zone_ids[item.zone_name] = hosted_zone_id(
                            self, f"Zone{_pascal(item.zone_name)}", item.zone_name
                        )
                    zone_id = zone_ids[item.zone_name]
                zone = route53.PublicHostedZone.from_public_hosted_zone_attributes(
                    self, f"{item.name}Zone", hosted_zone_id=zone_id, zone_name=item.zone_name
                )
                validation = acm.CertificateValidation.from_dns(zone)
            else:
                validation = acm.CertificateValidation.from_email()
            certificate = acm.Certificate(
                self,
                item.name,
                domain_name=item.domain_name,
                subject_alternative_names=list(item.subject_alternative_names) or None,
                certificate_name=full_name,
                validation=validation,
            )
            self.certificates[item.name] = certificate

            if item.secrets_manager is None:
                continue
            config = item.secrets_manager
            secret_name = config.name or naming(item.name, "acm-cert")
            plain = cdk.SecretValue.unsafe_plain_text
            secret = secretsmanager.Secret(
                self,
                f"{item.name}Metadata",
                secret_name=secret_name,
                description=config.description,
                encryption_key=kms.Key.from_key_arn(self, f"{item.name}MetadataKey", config.kms_key_arn)
                if config.kms_key_arn else None,
                removal_policy=config.removal_policy,
                # Public metadata only; ACM-issued private keys never leave ACM.
                secret_object_value={
                    "certificate_arn": plain(certificate.certificate_arn),
                    "certificate_domain_name": plain(item.domain_name),
                    "private_key_managed_by": plain("AWS ACM"),
                    "private_key_exportable": plain("false"),
                    "note": plain("ACM-issued public certificate private keys are not exportable."),
                },
            )
            cdk.Tags.of(secret).add("Name", naming(item.name, "acm-metadata"))
            self.secrets[item.name] = secret


def _pascal(text: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", text) if part)
