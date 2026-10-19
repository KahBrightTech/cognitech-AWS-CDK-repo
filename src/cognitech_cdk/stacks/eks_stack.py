"""EKS clusters, with everything around each cluster in nested stacks.

The parent stack holds the control plane and its kubectl provider. Per cluster,
nested stacks hold `Access` (access entries, RBAC, namespaces, Pod Identity),
`NodeGroups`, `CoreAddons`, `Karpenter`, `Addons` and `Apps` (the Load
Balancer Controller and Helm applications). The whole set deploys and rolls
back as one CloudFormation stack.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks_v2 as eks
from aws_cdk import aws_iam as iam
from aws_cdk.lambda_layer_kubectl_v36 import KubectlV36Layer
from constructs import Construct

from cognitech_cdk.common.environment import Environment
from cognitech_cdk.constructs.eks import EksCluster, EksProps, construct_id
from cognitech_cdk.constructs.eks_addons import ClusterAddonsProps, EksAddonsProps
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.stacks.base import EnvironmentStack
from cognitech_cdk.stacks.eks_addons import EksAddons

NESTED_DESCRIPTIONS = {
    "Access": "access entries, RBAC, namespaces and Pod Identity",
    "NodeGroups": "launch templates and managed node groups",
    "CoreAddons": "networking, DNS and Pod Identity add-ons",
    "Karpenter": "Karpenter controller, interruption queue and NodePool",
    "Addons": "storage, observability and DNS add-ons",
    "Apps": "Load Balancer Controller and Helm applications",
}


class EksStack(EnvironmentStack):
    def __init__(
        self,
        scope: Construct,
        construct_id_: str,
        *,
        environment: Environment,
        props: EksProps,
        network: Network,
        security_groups: Mapping[str, ec2.ISecurityGroup],
        roles: Mapping[str, iam.IRole],
        key_pairs: Mapping[str, ec2.IKeyPair] | None = None,
        addons: EksAddonsProps | None = None,
        certificates: Mapping[str, acm.ICertificate] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id_, environment=environment, **kwargs)
        self.props = props
        # One kubectl layer serves every cluster's handler in this stack.
        self.kubectl_layer = (
            KubectlV36Layer(self, "KubectlLayer")
            if any(cluster.kubectl for cluster in props.clusters) else None
        )

        self.eks_clusters: dict[str, EksCluster] = {}
        self.clusters: dict[str, eks.ICluster] = {}
        self.node_groups: dict[str, dict[str, eks.Nodegroup]] = {}
        self.launch_templates: dict[str, dict[str, ec2.CfnLaunchTemplate]] = {}
        self.access_entries: dict[str, dict[str, list[eks.CfnAccessEntry]]] = {}
        self.nested_stacks: dict[str, dict[str, cdk.NestedStack]] = {}
        self.addons: dict[str, EksAddons] = {}

        known = {cluster.name for cluster in props.clusters}
        for cluster_addons in (addons.clusters if addons else ()):
            if cluster_addons and cluster_addons.cluster not in known:
                raise ValueError(
                    f"eks_addons cluster: unknown reference '{cluster_addons.cluster}'. "
                    f"Configured: {', '.join(sorted(known)) or 'none'}."
                )
        by_cluster = {item.cluster: item for item in (addons.clusters if addons is not None else ())}
        for cluster_props in props.clusters:
            if cluster_props.karpenter is None:
                continue
            cluster_addons = by_cluster.get(cluster_props.name, ClusterAddonsProps(cluster=cluster_props.name))
            if cluster_addons.karpenter is not None:
                raise ValueError(
                    f"EKS cluster '{cluster_props.name}': configure karpenter under compute "
                    "or eks_addons, not both."
                )
            cluster_addons = replace(cluster_addons, karpenter=cluster_props.karpenter)
            cluster_addons.validate()
            if cluster_addons:
                by_cluster[cluster_props.name] = cluster_addons

        for cluster_props in props.clusters:
            name = cluster_props.name
            nested = self.nested_stacks.setdefault(name, {})

            def scope_for(group: str, name: str = name, nested: dict = nested) -> cdk.NestedStack:
                if group not in nested:
                    nested[group] = cdk.NestedStack(
                        self, f"{construct_id(name)}{group}",
                        description=f"{name} EKS {NESTED_DESCRIPTIONS[group]} for {environment.name}",
                    )
                return nested[group]

            built = EksCluster(
                self,
                construct_id(name),
                props=cluster_props,
                cluster_name=self.resource_name(name, "eks-cluster"),
                names=self.resource_name,
                network=network,
                security_groups=security_groups,
                roles=roles,
                key_pairs=key_pairs or {},
                kubectl_layer=self.kubectl_layer,
                access_scope=lambda scope_for=scope_for: scope_for("Access"),
                nodes_scope=lambda scope_for=scope_for: scope_for("NodeGroups"),
            )
            self.eks_clusters[name] = built
            self.clusters[name] = built.cluster
            self.node_groups[name] = built.node_groups
            self.launch_templates[name] = built.launch_templates
            self.access_entries[name] = built.access_entries
            self._outputs(name, built)

            if by_cluster.get(name):
                self.addons[name] = EksAddons(
                    self,
                    props=by_cluster[name],
                    cluster=built.cluster,
                    scope_for=scope_for,
                    after=list(built.node_groups.values()),
                    roles=roles,
                    network=network,
                    security_groups=security_groups,
                    certificates=certificates,
                )

    def _outputs(self, name: str, built: EksCluster) -> None:
        cluster = built.cluster
        prefix = construct_id(name)
        self.output(f"{prefix}ClusterName", cluster.cluster_name, f"{name}-eks-cluster-name")
        self.output(f"{prefix}ClusterArn", cluster.cluster_arn, f"{name}-eks-cluster-arn")
        self.output(f"{prefix}ClusterEndpoint", cluster.cluster_endpoint, f"{name}-eks-cluster-endpoint")
        self.output(
            f"{prefix}ClusterSecurityGroupId", cluster.cluster_security_group_id,
            f"{name}-eks-cluster-sg-id",
        )
        self.output(
            f"{prefix}OidcIssuer", cluster.cluster_open_id_connect_issuer_url,
            f"{name}-eks-oidc-issuer",
        )
        if built.oidc_provider is not None:
            self.output(
                f"{prefix}OidcProviderArn", built.oidc_provider.open_id_connect_provider_arn,
                f"{name}-eks-oidc-provider-arn",
            )
        for group_name, node_group in built.node_groups.items():
            group_prefix = f"{prefix}{construct_id(group_name)}"
            self.output(
                f"{group_prefix}NodeGroupName", node_group.nodegroup_name,
                f"{name}-{group_name}-nodegroup-name",
            )
            self.output(
                f"{group_prefix}NodeGroupArn", node_group.nodegroup_arn,
                f"{name}-{group_name}-nodegroup-arn",
            )
