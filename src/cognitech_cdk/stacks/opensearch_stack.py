"""OpenSearch domains, public (policy-restricted) or inside the network."""

from __future__ import annotations

from typing import Mapping

from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.constructs.opensearch import OpenSearchDomains, OpenSearchProps
from cognitech_cdk.constructs.rds import construct_id
from cognitech_cdk.stacks.base import EnvironmentStack


class OpenSearchStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        environment: Environment,
        props: OpenSearchProps,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
        certificates: Mapping[str, acm.ICertificate] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id_, environment=environment, **kwargs)
        self.opensearch = OpenSearchDomains(
            self,
            "OpenSearch",
            common=environment.common,
            prefix=environment.network.name,
            props=props,
            network=network,
            security_groups=security_groups,
            secrets=secrets,
            certificates=certificates,
        )
        self.domains: dict[str, opensearch.IDomain] = dict(self.opensearch.domains)
        # Master-user secrets (generated or referenced); ARNs only, never values.
        self.master_user_secrets: dict[str, secretsmanager.ISecret] = (
            self.opensearch.master_user_secrets
        )

        for name, domain in self.opensearch.domains.items():
            cid = construct_id(name)
            self.output(f"{cid}DomainName", domain.domain_name, f"{name}-opensearch-domain-name")
            self.output(f"{cid}DomainArn", domain.domain_arn, f"{name}-opensearch-domain-arn")
            self.output(
                f"{cid}DomainEndpoint", domain.domain_endpoint, f"{name}-opensearch-domain-endpoint"
            )
            if name in self.master_user_secrets:
                self.output(
                    f"{cid}MasterUserSecretArn",
                    self.master_user_secrets[name].secret_arn,
                    f"{name}-opensearch-master-secret-arn",
                    description="ARN of the master user secret (not its value)",
                )
