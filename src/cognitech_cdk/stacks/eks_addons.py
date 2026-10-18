"""EKS add-ons, Pod Identity roles and Helm-based controllers.

Built into nested stacks of `EksStack` (`CoreAddons`, `Karpenter`, `Addons`,
`Apps`) and created only after the node groups exist.

Install order on a small node group: core networking/DNS/identity add-ons,
then Karpenter and its NodePool, then everything else (which Karpenter can
schedule onto new nodes). Charts that create Ingresses or LoadBalancer Services
wait for the AWS Load Balancer Controller and its webhook.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

import aws_cdk as cdk
import yaml
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks_v2 as eks
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_sqs as sqs
from constructs import Construct, IDependable

from cognitech_cdk.common import schema
from cognitech_cdk.common.documents import render
from cognitech_cdk.constructs.eks import construct_id
from cognitech_cdk.constructs.eks_addons import (
    CORE_ADDONS,
    POD_IDENTITY_AGENT,
    ArgoCdProps,
    ClusterAddonsProps,
    GatewayApiProps,
    HostedChartProps,
    KarpenterProps,
    LoadBalancerProps,
    NativeAddonProps,
    NginxIngressProps,
    configuration_json,
)
from cognitech_cdk.constructs.network import Network
from cognitech_cdk.stacks.base import EnvironmentStack

POD_PRINCIPAL = "pods.eks.amazonaws.com"
INTERRUPTION_EVENTS = (
    ("ScheduledChange", "aws.health", "AWS Health Event"),
    ("SpotInterruption", "aws.ec2", "EC2 Spot Instance Interruption Warning"),
    ("Rebalance", "aws.ec2", "EC2 Instance Rebalance Recommendation"),
    ("InstanceStateChange", "aws.ec2", "EC2 Instance State-change Notification"),
    ("CapacityReservationInterruption", "aws.ec2", "EC2 Capacity Reservation Instance Interruption Warning"),
)


class EksAddons:
    """Builds one cluster's add-ons into nested stacks of the EKS stack.

    `scope_for(group)` returns (creating on first use) the nested stack for
    `CoreAddons`, `Karpenter`, `Addons` or `Apps`, so empty groups add nothing.
    """

    def __init__(
        self,
        stack: EnvironmentStack,
        *,
        props: ClusterAddonsProps,
        cluster: eks.ICluster,
        scope_for: Callable[[str], Construct],
        after: Sequence[IDependable] = (),
        roles: Mapping[str, iam.IRole] | None = None,
        network: Network | None = None,
        security_groups: Mapping[str, ec2.ISecurityGroup] | None = None,
        certificates: Mapping[str, acm.ICertificate] | None = None,
    ) -> None:
        self._stack = stack
        self._scope_for = scope_for
        self._after_nodes = list(after)
        self._roles = roles or {}
        self._network = network
        self._security_groups = security_groups or {}
        self._certificates = certificates or {}
        self.addons: dict[str, eks.CfnAddon] = {}
        self.pod_identity_roles: dict[str, iam.IRole] = {}
        self.helm_charts: dict[str, eks.HelmChart] = {}
        self.interruption_queue: sqs.IQueue | None = None
        self._cluster(props, cluster)

    # --- shared helpers -----------------------------------------------------

    def _pod_role(self, scope: Construct, logical: str, description: str) -> iam.Role:
        return iam.Role(
            scope,
            logical,
            assumed_by=iam.ServicePrincipal(POD_PRINCIPAL).with_session_tags(),
            description=description,
        )

    def _subnets(self, tier: str, label: str) -> list[str]:
        if self._network is None:
            raise ValueError(f"{label}: the network is required to place load balancers and nodes.")
        subnets = self._network.private_subnets if tier == "private" else self._network.public_subnets
        if not subnets:
            raise ValueError(f"{label}: the network has no {tier} subnets.")
        return [subnet.subnet_id for subnet in subnets]

    def _subnet_ids(self, tier: str, label: str) -> str:
        return ",".join(self._subnets(tier, label))

    def _group_ids(self, keys: tuple[str, ...], label: str) -> list[str]:
        return [
            schema.lookup(self._security_groups, key, f"{label}: security group").security_group_id
            for key in keys
        ]

    def _certificate_arn(self, props: LoadBalancerProps, label: str) -> str | None:
        if props.certificate is not None:
            return schema.lookup(self._certificates, props.certificate, f"{label}: certificate").certificate_arn
        return props.certificate_arn

    def _alb_annotations(self, props: LoadBalancerProps, label: str, *, health_path: str = "/") -> dict[str, str]:
        certificate = self._certificate_arn(props, label)
        annotations = {
            "alb.ingress.kubernetes.io/scheme": props.scheme,
            "alb.ingress.kubernetes.io/target-type": "ip",
            "alb.ingress.kubernetes.io/subnets": self._subnet_ids(props.subnet_tier, label),
            "alb.ingress.kubernetes.io/healthcheck-path": health_path,
            "alb.ingress.kubernetes.io/listen-ports": json.dumps(
                [{"HTTP": 80}, {"HTTPS": 443}] if certificate else [{"HTTP": 80}]
            ),
        }
        if certificate:
            annotations |= {
                "alb.ingress.kubernetes.io/certificate-arn": certificate,
                "alb.ingress.kubernetes.io/ssl-policy": props.ssl_policy,
                "alb.ingress.kubernetes.io/ssl-redirect": "443",
            }
        if props.security_groups:
            annotations |= {
                "alb.ingress.kubernetes.io/security-groups": ",".join(self._group_ids(props.security_groups, label)),
                "alb.ingress.kubernetes.io/manage-backend-security-group-rules": "true",
            }
        if props.group_name:
            annotations["alb.ingress.kubernetes.io/group.name"] = props.group_name
        return annotations

    def _nlb_annotations(self, props: LoadBalancerProps, label: str) -> dict[str, str]:
        prefix = "service.beta.kubernetes.io/aws-load-balancer"
        certificate = self._certificate_arn(props, label)
        annotations = {
            f"{prefix}-type": "external",
            f"{prefix}-nlb-target-type": "ip",
            f"{prefix}-scheme": props.scheme,
            f"{prefix}-subnets": self._subnet_ids(props.subnet_tier, label),
        }
        if certificate:
            annotations |= {
                f"{prefix}-ssl-cert": certificate,
                f"{prefix}-ssl-ports": "443",
                f"{prefix}-ssl-negotiation-policy": props.ssl_policy,
            }
        if props.security_groups:
            annotations |= {
                f"{prefix}-security-groups": ",".join(self._group_ids(props.security_groups, label)),
                f"{prefix}-manage-backend-security-group-rules": "true",
            }
        return annotations

    @staticmethod
    def _after(construct: Construct, dependencies: list[IDependable]) -> None:
        for dependency in dependencies:
            construct.node.add_dependency(dependency)

    # --- native add-ons -----------------------------------------------------

    def _addon_role(self, scope: Construct, cluster: str, addon: NativeAddonProps) -> iam.IRole:
        if addon.role is not None:
            return schema.lookup(self._roles, addon.role, f"EKS add-ons '{cluster}', {addon.name} role")
        spec = addon.spec
        role = self._pod_role(
            scope, f"{construct_id(addon.name)}Role", f"EKS Pod Identity for {addon.name} on {cluster}"
        )
        for policy in spec.managed_policies:
            role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name(policy))
        if spec.route53 is not None:
            zones = [
                f"arn:{cdk.Aws.PARTITION}:route53:::hostedzone/{zone}"
                for zone in addon.hosted_zone_ids or ("*",)
            ]
            role.add_to_policy(iam.PolicyStatement(
                actions=["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"],
                resources=zones,
            ))
            if spec.route53 == "external-dns":
                role.add_to_policy(iam.PolicyStatement(
                    actions=["route53:ListHostedZones", "route53:ListTagsForResources"],
                    resources=["*"],
                ))
            else:
                role.add_to_policy(iam.PolicyStatement(
                    actions=["route53:GetChange"],
                    resources=[f"arn:{cdk.Aws.PARTITION}:route53:::change/*"],
                ))
                role.add_to_policy(iam.PolicyStatement(
                    actions=["route53:ListHostedZonesByName"], resources=["*"]
                ))
        return role

    def _native_addon(
        self, scope: Construct, name: str, addon: NativeAddonProps, cluster: eks.ICluster,
        roles: dict[str, iam.IRole],
    ) -> eks.CfnAddon:
        associations = None
        if addon.pod_identity:
            role = self._addon_role(scope, name, addon)
            roles[addon.name] = role
            associations = [eks.CfnAddon.PodIdentityAssociationProperty(
                role_arn=role.role_arn, service_account=addon.spec.service_account
            )]
        resource = eks.CfnAddon(
            scope,
            f"{construct_id(addon.name)}Addon",
            addon_name=addon.name,
            cluster_name=cluster.cluster_name,
            addon_version=addon.version,
            configuration_values=configuration_json(addon.configuration_values),
            resolve_conflicts=addon.resolve_conflicts,
            preserve_on_delete=addon.preserve_on_delete,
            pod_identity_associations=associations,
        )
        prefix = construct_id(name)
        self._stack.output(
            f"{prefix}{construct_id(addon.name)}AddonArn", resource.attr_arn,
            f"{name}-{addon.name}-addon-arn",
        )
        if addon.name in roles:
            self._stack.output(
                f"{prefix}{construct_id(addon.name)}RoleArn", roles[addon.name].role_arn,
                f"{name}-{addon.name}-role-arn",
            )
        return resource

    # --- per cluster --------------------------------------------------------

    def _cluster(self, props: ClusterAddonsProps, cluster: eks.ICluster) -> None:
        name = props.cluster
        label = f"EKS add-ons '{name}'"
        prefix = construct_id(name)
        addons: dict[str, eks.CfnAddon] = {}
        roles: dict[str, iam.IRole] = {}
        charts: dict[str, eks.HelmChart] = {}

        if (props.load_balancer_controller.enabled or props.helm_charts or props.charts
                or props.storage_class) and getattr(cluster, "kubectl_provider", None) is None:
            raise ValueError(
                f"{label}: Helm charts and manifests need the cluster's kubectl provider "
                "(set kubectl: true on the cluster)."
            )

        core = [addon for addon in props.addons if addon.name in CORE_ADDONS]
        rest = [addon for addon in props.addons if addon.name not in CORE_ADDONS]
        core.sort(key=lambda addon: addon.name != POD_IDENTITY_AGENT)
        for addon in core:
            addons[addon.name] = self._native_addon(self._scope_for("CoreAddons"), name, addon, cluster, roles)
            # Core add-ons such as CoreDNS only become active once nodes exist.
            self._after(addons[addon.name], self._after_nodes)
        agent = addons.get(POD_IDENTITY_AGENT)
        for addon in core:
            if agent is not None and addon.pod_identity and addon.name != POD_IDENTITY_AGENT:
                addons[addon.name].node.add_dependency(agent)

        # Everything after this point waits for node capacity when Karpenter is configured.
        capacity: list[IDependable] = list(addons.values()) or list(self._after_nodes)
        if props.karpenter is not None and props.karpenter.enabled:
            gate = self._karpenter(self._scope_for("Karpenter"), name, props.karpenter, cluster, agent, list(addons.values()), roles)
            charts["karpenter"] = gate[0]
            capacity = [gate[1]]

        for addon in rest:
            resource = self._native_addon(self._scope_for("Addons"), name, addon, cluster, roles)
            if agent is not None and addon.pod_identity:
                resource.node.add_dependency(agent)
            self._after(resource, capacity)
            addons[addon.name] = resource

        ready: list[IDependable] = [*capacity, *(addons[a.name] for a in rest)]
        if props.storage_class is not None:
            storage = props.storage_class
            manifest = eks.KubernetesManifest(
                self._scope_for("Addons"), "DefaultStorageClass", cluster=cluster, overwrite=True,
                manifest=[{
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "StorageClass",
                    "metadata": {
                        "name": storage.name,
                        "annotations": {"storageclass.kubernetes.io/is-default-class": str(storage.default).lower()},
                    },
                    "provisioner": "ebs.csi.aws.com",
                    "volumeBindingMode": "WaitForFirstConsumer",
                    "allowVolumeExpansion": True,
                    "parameters": {"type": storage.volume_type, "encrypted": "true"},
                }],
            )
            manifest.node.add_dependency(addons["aws-ebs-csi-driver"])
            ready.append(manifest)

        controller = props.load_balancer_controller
        if controller.enabled:
            chart = self._load_balancer_controller(self._scope_for("Apps"), name, props, cluster, agent, roles)
            self._after(chart, capacity)
            charts["aws-load-balancer-controller"] = chart
            ready.append(chart)

        if props.argocd is not None and props.argocd.enabled:
            charts["argocd"] = self._chart(
                self._scope_for("Apps"), "ArgoCd", cluster, props.argocd, "argo-cd", "https://argoproj.github.io/argo-helm",
                self._argocd_values(props.argocd, f"{label}, argocd"), ready,
            )
        for item in props.nginx_ingress:
            if item.enabled:
                charts[item.release] = self._chart(
                    self._scope_for("Apps"), f"NginxIngress{construct_id(item.name)}", cluster, item, "nginx-ingress",
                    "oci://ghcr.io/nginx/charts/nginx-ingress", self._nginx_values(item, f"{label}, nginx_ingress"), ready,
                )
        if props.gateway_api is not None and props.gateway_api.enabled:
            charts["nginx-gateway-fabric"] = self._gateway_api(self._scope_for("Apps"), label, cluster, props.gateway_api, ready)
        if props.kubecost is not None and props.kubecost.enabled:
            charts["kubecost"] = self._chart(
                self._scope_for("Apps"), "Kubecost", cluster, props.kubecost, "cost-analyzer",
                "https://kubecost.github.io/cost-analyzer/",
                self._kubecost_values(props.kubecost, f"{label}, kubecost"), ready,
            )
        if props.awx_operator is not None and props.awx_operator.enabled:
            charts["awx-operator"] = self._chart(
                self._scope_for("Apps"), "AwxOperator", cluster, props.awx_operator, "awx-operator",
                "https://ansible-community.github.io/awx-operator-helm/",
                self._awx_values(props.awx_operator, f"{label}, awx_operator"), ready,
            )

        for helm in props.helm_charts:
            chart = eks.HelmChart(
                self._scope_for("Apps"),
                f"{construct_id(helm.name)}Chart",
                cluster=cluster,
                chart=helm.chart,
                repository=helm.repository,
                release=helm.release,
                namespace=helm.namespace,
                create_namespace=helm.create_namespace,
                version=helm.version,
                values=dict(helm.values) or None,
                wait=helm.wait,
                timeout=cdk.Duration.minutes(helm.timeout_minutes),
            )
            self._after(chart, ready)
            charts[helm.name] = chart

        self.addons = addons
        self.pod_identity_roles = roles
        self.helm_charts = charts
        for role_name, role in roles.items():
            if role_name in ("aws-load-balancer-controller", "karpenter"):
                self._stack.output(f"{prefix}{construct_id(role_name)}RoleArn", role.role_arn, f"{name}-{role_name}-role-arn")

    def _chart(
        self, scope: Construct, logical: str, cluster: eks.ICluster, props: Any, chart: str,
        repository: str, values: dict[str, Any], dependencies: list[IDependable],
    ) -> eks.HelmChart:
        merged = _merge(values, dict(props.values))
        resource = eks.HelmChart(
            scope, logical,
            cluster=cluster,
            chart=chart,
            repository=repository,
            release=props.release,
            namespace=props.namespace,
            create_namespace=True,
            version=props.chart_version,
            values=merged,
            wait=False,
            timeout=cdk.Duration.minutes(15),
        )
        self._after(resource, dependencies)
        return resource

    # --- AWS Load Balancer Controller ----------------------------------------

    def _load_balancer_controller(
        self, scope: Construct, name: str, props: ClusterAddonsProps, cluster: eks.ICluster,
        agent: eks.CfnAddon | None, roles: dict[str, iam.IRole],
    ) -> eks.HelmChart:
        controller = props.load_balancer_controller
        if controller.role is not None:
            role = schema.lookup(
                self._roles, controller.role, f"EKS add-ons '{name}', load balancer controller role"
            )
        else:
            role = self._pod_role(
                scope, "AwsLoadBalancerControllerRole",
                f"EKS Pod Identity for the AWS Load Balancer Controller on {name}",
            )
            role.attach_inline_policy(iam.Policy(
                scope, "AwsLoadBalancerControllerPolicy",
                document=iam.PolicyDocument.from_json(dict(controller.policy or {})),
            ))
        roles["aws-load-balancer-controller"] = role
        association = eks.CfnPodIdentityAssociation(
            scope,
            "AwsLoadBalancerControllerPodIdentity",
            cluster_name=cluster.cluster_name,
            namespace=controller.namespace,
            service_account=controller.service_account,
            role_arn=role.role_arn,
        )
        if agent is not None:
            association.node.add_dependency(agent)
        values = {
            "clusterName": cluster.cluster_name,
            "region": self._stack.region,
            "vpcId": cluster.vpc.vpc_id,
            "serviceAccount": {"create": True, "name": controller.service_account},
            **controller.values,
        }
        # Waits so its admission webhook is serving before charts create Ingresses/Services.
        chart = eks.HelmChart(
            scope,
            "AwsLoadBalancerController",
            cluster=cluster,
            chart="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            release="aws-load-balancer-controller",
            namespace=controller.namespace,
            version=controller.chart_version,
            values=values,
            wait=True,
            timeout=cdk.Duration.minutes(15),
        )
        chart.node.add_dependency(association)
        return chart

    # --- Karpenter -----------------------------------------------------------

    def _karpenter(
        self, scope: Construct, name: str, props: KarpenterProps, cluster: eks.ICluster,
        agent: eks.CfnAddon | None, core: list[eks.CfnAddon], roles: dict[str, iam.IRole],
    ) -> tuple[eks.HelmChart, eks.KubernetesManifest]:
        label = f"EKS add-ons '{name}', karpenter"
        node_role = schema.lookup(self._roles, props.node_role, f"{label}: node_role")
        queue = sqs.Queue(
            scope, "KarpenterInterruptionQueue",
            retention_period=cdk.Duration.minutes(5),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        queue.add_to_resource_policy(iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            principals=[iam.ServicePrincipal("events.amazonaws.com"), iam.ServicePrincipal("sqs.amazonaws.com")],
            resources=[queue.queue_arn],
        ))
        self.interruption_queue = queue
        for logical, source, detail in INTERRUPTION_EVENTS:
            events.CfnRule(
                scope, f"Karpenter{logical}Rule",
                event_pattern={"source": [source], "detail-type": [detail]},
                targets=[events.CfnRule.TargetProperty(id="KarpenterInterruptionQueue", arn=queue.queue_arn)],
            )

        role = self._pod_role(scope, "KarpenterControllerRole", f"EKS Pod Identity for Karpenter on {name}")
        roles["karpenter"] = role
        # The official policy uses the cluster name inside condition keys, which only
        # Fn::Sub over the whole document can express.
        text = json.dumps(dict(props.policy))
        for placeholder, variable in (
            ("partition", "${AWS::Partition}"), ("region", "${AWS::Region}"),
            ("account_number", "${AWS::AccountId}"), ("cluster_name", "${ClusterName}"),
            ("node_role_arn", "${NodeRoleArn}"), ("queue_arn", "${QueueArn}"),
        ):
            text = text.replace(f"[[{placeholder}]]", variable)
        iam.CfnPolicy(
            scope, "KarpenterControllerPolicy",
            policy_name="KarpenterController",
            roles=[role.role_name],
            policy_document=cdk.Fn.sub(text, {
                "ClusterName": cluster.cluster_name,
                "NodeRoleArn": node_role.role_arn,
                "QueueArn": queue.queue_arn,
            }),
        )
        association = eks.CfnPodIdentityAssociation(
            scope, "KarpenterPodIdentity",
            cluster_name=cluster.cluster_name, namespace=props.namespace,
            service_account="karpenter", role_arn=role.role_arn,
        )
        if agent is not None:
            association.node.add_dependency(agent)
        if props.create_node_access_entry:
            eks.CfnAccessEntry(
                scope, "KarpenterNodeAccessEntry",
                cluster_name=cluster.cluster_name, principal_arn=node_role.role_arn, type="EC2_LINUX",
            )

        chart = eks.HelmChart(
            scope, "Karpenter",
            cluster=cluster,
            chart="karpenter",
            repository="oci://public.ecr.aws/karpenter/karpenter",
            release="karpenter",
            namespace=props.namespace,
            version=props.chart_version,
            values=_merge({
                "replicas": props.replicas,
                "serviceAccount": {"name": "karpenter"},
                "settings": {"clusterName": cluster.cluster_name, "interruptionQueue": queue.queue_name},
            }, dict(props.values)),
            wait=True,
            timeout=cdk.Duration.minutes(15),
        )
        chart.node.add_dependency(association)
        self._after(chart, list(core))

        pool = props.node_pool
        group_ids = [{"id": cluster.cluster_security_group_id}] + [
            {"id": group} for group in self._group_ids(props.security_groups, label)
        ]
        manifests = eks.KubernetesManifest(
            scope, "KarpenterNodePool", cluster=cluster, overwrite=True,
            manifest=[
                {
                    "apiVersion": "karpenter.k8s.aws/v1",
                    "kind": "EC2NodeClass",
                    "metadata": {"name": "default"},
                    "spec": {
                        "role": node_role.role_name,
                        "amiSelectorTerms": [{"alias": "al2023@latest"}],
                        "subnetSelectorTerms": [
                            {"id": subnet} for subnet in self._subnets(props.subnet_tier, label)
                        ],
                        "securityGroupSelectorTerms": group_ids,
                        "metadataOptions": {"httpTokens": "required", "httpPutResponseHopLimit": 2},
                        "tags": {**self._stack.config.common.tags, "Name": self._stack.resource_name(name, "karpenter-node")},
                    },
                },
                {
                    "apiVersion": "karpenter.sh/v1",
                    "kind": "NodePool",
                    "metadata": {"name": "default"},
                    "spec": {
                        "template": {"spec": {
                            "nodeClassRef": {"group": "karpenter.k8s.aws", "kind": "EC2NodeClass", "name": "default"},
                            "requirements": [
                                {"key": "karpenter.k8s.aws/instance-category", "operator": "In",
                                 "values": list(pool.instance_categories)},
                                {"key": "karpenter.sh/capacity-type", "operator": "In",
                                 "values": list(pool.capacity_types)},
                                {"key": "kubernetes.io/arch", "operator": "In", "values": list(pool.architectures)},
                            ],
                            "expireAfter": pool.expire_after,
                        }},
                        "limits": {"cpu": str(pool.cpu_limit)},
                        "disruption": {
                            "consolidationPolicy": "WhenEmptyOrUnderutilized",
                            "consolidateAfter": pool.consolidate_after,
                        },
                    },
                },
            ],
        )
        manifests.node.add_dependency(chart)
        self._stack.output(f"{construct_id(name)}KarpenterQueueName", queue.queue_name, f"{name}-karpenter-queue-name")
        return chart, manifests

    # --- chart values -------------------------------------------------------

    def _ingress(self, host: str, props: LoadBalancerProps, label: str, *, health_path: str = "/") -> dict[str, Any]:
        return {
            "enabled": True,
            "ingressClassName": "alb",
            "annotations": self._alb_annotations(props, label, health_path=health_path),
            "host": host,
        }

    def _argocd_values(self, props: ArgoCdProps, label: str) -> dict[str, Any]:
        ingress = self._ingress(props.host, props.ingress, label, health_path="/healthz")
        scheme = "https" if props.ingress.tls else "http"
        values: dict[str, Any] = {
            "global": {"domain": props.host},
            # TLS ends at the ALB, so the server speaks plain HTTP inside the VPC.
            "configs": {
                "params": {"server.insecure": True},
                "cm": {"url": f"{scheme}://{props.host}"},
            },
            "server": {
                "replicas": props.server_replicas,
                "ingress": {
                    "enabled": True,
                    "ingressClassName": "alb",
                    "hostname": props.host,
                    "annotations": ingress["annotations"],
                    "path": "/",
                    "pathType": "Prefix",
                },
            },
            "redis-ha": {"enabled": props.ha},
        }
        if props.ha:
            values["controller"] = {"replicas": 1}
            values["repoServer"] = {"replicas": 2}
            values["applicationSet"] = {"replicas": 2}
        sso = props.sso
        if sso.enabled:
            callback = f"{scheme}://{props.host}/api/dex/callback"
            dex = {"connectors": [{
                "type": "saml",
                "id": "aws",
                "name": sso.connector_name,
                "config": {
                    "ssoURL": sso.sso_url,
                    **({"ssoIssuer": sso.sso_issuer} if sso.sso_issuer else {}),
                    "caData": sso.ca_data,
                    "redirectURI": callback,
                    "entityIssuer": callback,
                    "usernameAttr": sso.username_attr,
                    "emailAttr": sso.email_attr,
                    "groupsAttr": sso.groups_attr,
                    "nameIDPolicyFormat": sso.name_id_policy_format,
                },
            }]}
            values["configs"]["cm"]["dex.config"] = yaml.safe_dump(dex, sort_keys=False)
            values["configs"]["rbac"] = {
                "policy.default": sso.rbac_default_policy,
                "policy.csv": sso.rbac_policies,
                "scopes": "[groups, email]",
            }
        return values

    def _nginx_values(self, props: NginxIngressProps, label: str) -> dict[str, Any]:
        service: dict[str, Any] = {
            "type": "LoadBalancer",
            "annotations": self._nlb_annotations(props.service, f"{label} '{props.name}'"),
        }
        if props.service.tls:
            # TLS ends at the NLB; forward 443 to the controller's HTTP port.
            service["httpsPort"] = {"enable": True, "port": 443, "targetPort": 80}
        return {"controller": {
            "replicaCount": props.replica_count,
            "ingressClass": {
                "name": props.ingress_class_name,
                "create": True,
                "setAsDefaultIngress": props.default_class,
            },
            "service": service,
        }}

    def _kubecost_values(self, props: HostedChartProps, label: str) -> dict[str, Any]:
        if props.host is None:
            return {}
        ingress = self._ingress(props.host, props.ingress, label, health_path="/healthz")
        return {"ingress": {
            "enabled": True,
            "className": "alb",
            "annotations": ingress["annotations"],
            "hosts": [props.host],
            "paths": ["/"],
            "pathType": "Prefix",
        }}

    def _awx_values(self, props: HostedChartProps, label: str) -> dict[str, Any]:
        if not props.create_instance:
            return {}
        spec: dict[str, Any] = {"admin_user": "admin", "service_type": "ClusterIP"}
        if props.host is not None:
            annotations = self._ingress(props.host, props.ingress, label, health_path="/api/v2/ping/")["annotations"]
            spec |= {
                "ingress_type": "ingress",
                "ingress_class_name": "alb",
                "hostname": props.host,
                # The AWX CRD takes annotations as a YAML string.
                "ingress_annotations": "\n".join(f"{key}: '{value}'" for key, value in annotations.items()),
            }
        return {"AWX": {"enabled": True, "name": "awx", "spec": spec}}

    def _gateway_api(
        self, scope: Construct, label: str, cluster: eks.ICluster, props: GatewayApiProps,
        dependencies: list[IDependable],
    ) -> eks.HelmChart:
        # The upstream CRD bundle is too large to inline, so it ships as a chart asset.
        crds = eks.HelmChart(
            scope, "GatewayApiCrds",
            cluster=cluster,
            chart_asset=s3_assets.Asset(scope, "GatewayApiCrdsChart", path=props.crds_chart),
            release="gateway-api-crds",
            namespace=props.namespace,
            create_namespace=True,
            wait=False,
        )
        self._after(crds, dependencies)
        values: dict[str, Any] = {
            "nginxGateway": {"gatewayClassName": props.gateway_class_name, "replicas": props.replicas},
        }
        if props.service is not None:
            values["nginx"] = {"service": {
                "type": "LoadBalancer",
                "patches": [{
                    "type": "StrategicMerge",
                    "value": {"metadata": {"annotations": self._nlb_annotations(props.service, f"{label}, gateway_api")}},
                }],
            }}
        chart = self._chart(
            scope, "NginxGatewayFabric", cluster, props, "nginx-gateway-fabric", "oci://ghcr.io/nginx/charts/nginx-gateway-fabric",
            values, dependencies,
        )
        chart.node.add_dependency(crds)
        return chart


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge user chart values over generated ones."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(dict(result[key]), value)
        else:
            result[key] = value
    return result
