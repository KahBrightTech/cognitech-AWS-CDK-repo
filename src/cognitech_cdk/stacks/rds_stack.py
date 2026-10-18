"""RDS database instances placed in the network's subnets."""

from __future__ import annotations

from typing import Mapping

from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.constructs.rds import RdsInstances, RdsProps, construct_id
from cognitech_cdk.stacks.base import EnvironmentStack


class RdsStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        environment: Environment,
        props: RdsProps,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        secrets: Mapping[str, secretsmanager.ISecret] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id_, environment=environment, **kwargs)
        self.rds = RdsInstances(
            self,
            "Rds",
            common=environment.common,
            prefix=environment.network.name,
            props=props,
            network=network,
            security_groups=security_groups,
            secrets=secrets,
        )
        self.instances: dict[str, rds.DatabaseInstance] = self.rds.instances
        self.replicas: dict[str, rds.DatabaseInstanceReadReplica] = self.rds.replicas
        # Credential secrets for downstream task definitions; ARNs only, never values.
        self.secrets: dict[str, secretsmanager.ISecret] = self.rds.secrets

        for name, instance in self.instances.items():
            cid = construct_id(name)
            self.output(f"{cid}RdsIdentifier", instance.instance_identifier, f"{name}-rds-identifier")
            self.output(f"{cid}RdsArn", instance.instance_arn, f"{name}-rds-arn")
            self.output(
                f"{cid}RdsEndpoint", instance.db_instance_endpoint_address, f"{name}-rds-endpoint"
            )
            self.output(f"{cid}RdsPort", instance.db_instance_endpoint_port, f"{name}-rds-port")
            self.output(
                f"{cid}RdsSubnetGroupName",
                self.rds.subnet_groups[name].subnet_group_name,
                f"{name}-rds-subnet-group-name",
            )
            if name in self.secrets:
                self.output(
                    f"{cid}RdsSecretArn", self.secrets[name].secret_arn, f"{name}-rds-secret-arn",
                    description="ARN of the master credentials secret (not its value)",
                )
        for name, replica in self.replicas.items():
            cid = construct_id(name)
            self.output(
                f"{cid}RdsReplicaIdentifier", replica.instance_identifier,
                f"{name}-rds-replica-identifier",
            )
            self.output(f"{cid}RdsReplicaArn", replica.instance_arn, f"{name}-rds-replica-arn")
            self.output(
                f"{cid}RdsReplicaEndpoint", replica.db_instance_endpoint_address,
                f"{name}-rds-replica-endpoint",
            )
