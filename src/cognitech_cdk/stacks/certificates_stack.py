"""Public ACM certificates validated through Route 53 DNS."""

from __future__ import annotations

import re

from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.certificates import Certificates, CertificatesProps
from cognitech_cdk.stacks.base import EnvironmentStack


def _logical(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name) if part)


class CertificatesStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: Environment,
        props: CertificatesProps,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, environment=environment, **kwargs)
        built = Certificates(self, "Certificates", props=props, naming=self.resource_name)
        self.certificates: dict[str, acm.ICertificate] = dict(built.certificates)
        self.secrets: dict[str, secretsmanager.ISecret] = dict(built.secrets)

        domains = {item.name: item.domain_name for item in props.certificates}
        for name, certificate in built.certificates.items():
            logical = _logical(name)
            self.output(f"{logical}CertificateArn", certificate.certificate_arn, f"{name}-certificate-arn")
            self.output(f"{logical}CertificateDomainName", domains[name], f"{name}-certificate-domain-name")
        for name, secret in built.secrets.items():
            logical = _logical(name)
            self.output(f"{logical}CertificateSecretArn", secret.secret_arn, f"{name}-certificate-secret-arn")
            self.output(f"{logical}CertificateSecretName", secret.secret_name, f"{name}-certificate-secret-name")
