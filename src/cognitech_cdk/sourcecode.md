# Source Code Guide

This guide explains the Python code in the `cognitech_cdk` package in everyday
language. The package uses AWS CDK (the Cloud Development Kit) to describe AWS
network, security group and IAM resources as code. CDK turns that description into a CloudFormation
template; deploying that template is a separate step.

## The short version

The package has three main jobs:

1. Read configuration for a named deployment environment.
2. Check the shared settings and network address ranges before deployment.
3. Define a VPC, its subnets, gateways, names, tags, and exported stack outputs.

The code is connected roughly like this:

```text
deployment env.yaml
        |
        v
common/environment.py ----> common/config.py
        |                    constructs/network.py  (NetworkProps)
        |                    constructs/security.py (SecurityGroupProps)
        |                    constructs/iam.py      (IamProps)
        v
stacks/network_stack.py ---> constructs/network.py  (Network)
stacks/security_stack.py --> constructs/security.py (SecurityGroups)
stacks/iam_stack.py -------> constructs/iam.py      (Iam)
```

The app entry point, `app.py`, is one level above this package. It chooses an
environment, calls the environment loader, creates the stack, and asks CDK to
synthesize the deployment template.

## `common/config.py` - shared names, tags, and settings

This module contains information that can be reused by more than one stack or
construct.

### Imports and constants

- `dataclass` and `field` are Python helpers for defining simple data objects.
- `Mapping` describes a read-only-style mapping type for tags.
- `ORDINALS` gives the human-readable position names used for resources in
  availability zones: `primary`, `secondary`, `tertiary`, and `quaternary`.
- `MAX_AZS` is the number of names in that list, so the supported maximum is
  four availability zones.
- `REQUIRED_TAGS` lists the tags every environment must provide. The `Name`
  tag is intentionally not in this list because each individual resource gets
  its own generated name.

### `ordinal(index)`

This small helper returns the position name at a zero-based index. For example,
index `0` returns `primary` and index `1` returns `secondary`. The network
configuration checks the supported availability-zone count before using these
positions.

### The `Common` data class

`Common` stores values used across resources:

- `account_name` and `region_prefix` are the main pieces of generated names.
- `tags` contains the shared tags.
- `global_` stores the configuration's `global` setting. The trailing
  underscore lets Python use a field name that does not collide with the
  `global` keyword.
- `account_name_abr` is an optional shorter account name for resources with
  shorter name limits.
- `environment_abr` stores an optional environment abbreviation.

The class is frozen, which means its fields cannot be reassigned after the
object is created.

### `Common.name(*parts)`

Builds a resource name by joining the account name, region prefix, and any
non-empty extra parts with hyphens. For example, parts describing a sample VPC
might produce a name shaped like:

```text
example-account-use1-sample-vpc
```

Empty optional parts are skipped so the result does not contain accidental
double hyphens.

### `Common.abbreviated_name(*parts)`

Builds a name in the same way as `name`, but uses `account_name_abr` when that
abbreviation has been supplied. If it is empty, the full account name is used.
This is useful for AWS resources that impose tighter name-length limits.

### `Common.from_dict(raw, env_name)`

Converts the `common` section from YAML into a `Common` object. It:

1. Converts tag keys and values to strings.
2. Rejects a shared `Name` tag, because names are generated separately for each
   resource.
3. Checks that every tag listed in `REQUIRED_TAGS` is present and non-empty.
4. Reads the account name and region prefix, plus optional flags and
   abbreviations.

When required tags are missing, the error includes the environment name to make
the configuration problem easier to locate.

## `common/environment.py` - loading an environment

This module locates and reads environment-specific YAML files and packages their
values into objects that the CDK stack can use.

### Imports and path constants

- `Path` handles filesystem paths.
- `Any` and `Mapping` describe the YAML data as it is loaded.
- `yaml` reads the YAML file.
- `Common` and `NetworkProps` convert the shared and network sections into
  typed Python objects.
- `DEPLOYMENTS_DIR` points to the repository's `deployments` directory,
  calculated from this module's location.
- `ENV_FILE` is the filename expected inside each environment folder:
  `env.yaml`.

### The `Environment` data class

`Environment` is the combined configuration for one deployment. It holds the
environment name, AWS account ID, AWS region, shared `Common` settings, and
network `NetworkProps`, optional security groups, and optional IAM `IamProps`.

Its `cdk_env` property returns the account and region as a mapping in the shape
expected by CDK. This allows a stack to be tied to the intended AWS account and
region.

### `load_environment(env_name, root=DEPLOYMENTS_DIR)`

Loads one environment from:

```text
<root>/<env_name>/env.yaml
```

The function checks that the file exists first. If it does not, it reports the
expected path and the environment names it could find. It then reads the YAML
using `yaml.safe_load`, which parses data without allowing arbitrary Python
object construction.

Finally, it builds an `Environment` object. The `common` section is validated
through `Common.from_dict`; the `network` section is converted through
`NetworkProps.from_dict`.
The optional `iam` section is converted through `IamProps.from_dict`, which
checks role trust, policy names, profile references and duplicate names.

### `list_environments(root=DEPLOYMENTS_DIR)`

Looks for folders under `deployments/` that contain an `env.yaml` file and
returns their folder names in sorted order. If the deployments directory does
not exist, it returns an empty list.

## `constructs/network.py` - network settings and AWS resources

This is the main network module. It has two parts:

- `NetworkProps` describes and validates the settings.
- `Network` uses those settings to define AWS resources in the CDK app.

### Module documentation, imports, and default

The opening documentation describes the intended network: a VPC with an
Internet Gateway and public subnets, plus optional private subnets, NAT
gateways, and Elastic IPs. It also explains why the subnets are created
explicitly: the configuration can specify exact CIDR ranges, rather than only a
subnet prefix length.

`ipaddress` performs IP network and subnet calculations. The remaining imports
provide the data-class/type helpers, AWS CDK EC2 resource definitions,
construct base class, and shared naming helpers.

`DEFAULT_CIDR_MASK` is `24`. When a subnet range is not explicitly configured,
the code tries to carve out a subnet with this prefix length.

### The `NetworkProps` data class

This object holds the network settings read from YAML:

- `name` and `cidr_block` identify the network and the overall VPC address
  range.
- `azs` is the number of availability zones; it defaults to two.
- `private_subnets` turns private subnets and their required NAT gateways on or
  off; it defaults to off.
- `nat_gateways` optionally sets how many NAT gateways to create.
- `public_subnet_cidrs` and `private_subnet_cidrs` optionally provide the exact
  subnet address ranges.
- `cidr_mask` sets the prefix length for automatically selected subnet ranges.
- `public_subnet_name` and `private_subnet_name` customize the name parts used
  for each subnet tier.

The data class is frozen so the settings stay consistent after validation.

### `NetworkProps.__post_init__()`

Python calls this method immediately after creating a `NetworkProps` object.
It catches common configuration problems before CDK deployment:

1. The availability-zone count must be between one and four.
2. A configured NAT gateway count cannot be negative.
3. Private subnets must have at least one NAT gateway.
4. The subnet CIDRs are calculated immediately, which also checks that the
   configured ranges are valid.

Failing early gives a clear Python configuration error instead of waiting for
AWS to reject the synthesized infrastructure.

### `nat_gateway_count`

This property decides the effective number of NAT gateways:

- With private subnets disabled, the count is zero.
- With private subnets enabled and no explicit count, it is one per
  availability zone.
- With an explicit count, it is capped at the number of availability zones.

The earlier validation makes sure private subnets cannot continue with an
effective count of zero.

### `subnet_cidrs()`

Returns two tuples: the public subnet CIDRs and the private subnet CIDRs. There
should be one subnet range per availability zone for each enabled tier.

The method first turns the VPC range into an IP network and records any
explicitly configured ranges. Every explicit range is checked to make sure it:

- Has one entry per availability zone.
- Is inside the VPC range.
- Does not overlap another explicit subnet range.

Only after reserving all explicit ranges does the method fill in any
unspecified ranges. This prevents automatically selected subnets from
colliding with explicit ranges, regardless of which tier supplied them.

The small `resolve()` helper handles one tier. It normalizes explicit ranges, or
repeatedly asks `_next_free()` for an available range. If private subnets are
disabled, the requested number of private ranges is zero, so no private subnet
CIDRs are returned.

### `_next_free(vpc, taken)`

Checks the VPC's possible subnets at the configured prefix length in ascending
network order. It returns the first candidate that does not overlap anything
already reserved. If no matching free range remains, it raises a helpful
`ValueError`.

### `NetworkProps.from_dict(raw)`

Converts the YAML `network` mapping to a `NetworkProps` object. It converts
numbers and booleans to their expected Python types, applies defaults when
optional settings are absent, and converts CIDR lists to tuples. Creating the
object then runs `__post_init__()` and its validation.

### The `Network` construct and its constructor

`Network` is a reusable CDK construct: a named group of related resources that a
stack can include.

The constructor:

1. Initializes the CDK construct.
2. Saves the shared settings and network properties.
3. Builds the availability-zone names from the AWS region and the requested
   number of zones.
4. Gets the checked public and private subnet ranges.
5. Creates the VPC with DNS support enabled.
6. Turns off CDK's automatic gateway and subnet creation so this module can
   create each resource with the exact configured CIDR.
7. Creates the Internet Gateway, public subnets, NAT gateways, and private
   subnets in dependency order.
8. Applies names to the gateways after they have been created.

If private subnets are disabled, the private-subnet list and NAT-gateway list
are empty.

### `subnet_selection(subnet_type)`

Returns a CDK subnet selection containing either the public subnet objects or
the private subnet objects. This can be passed to other CDK resources that need
to choose subnets, such as an application load balancer or a database. Call it
with `"public"` for public subnets; other values select the private subnet list.

### `_add_internet_gateway()`

Creates an Internet Gateway and a separate attachment connecting it to the VPC.
The attachment is needed so public subnets can use the gateway for internet
routes. Both CDK resource objects are returned for later use.

### `_add_public_subnets(cidrs)`

Creates one public subnet for each availability-zone/CIDR pair. Each subnet:

- Is placed in its matching availability zone.
- Uses the assigned CIDR.
- Maps public IP addresses onto launched instances.
- Gets a default route through the Internet Gateway.
- Gets a generated `Name` tag, along with a matching route-table name.

The method returns the created public subnet objects.

### `_add_private_subnets(cidrs)`

Creates one private subnet for each provided availability-zone/CIDR pair.
Private subnets do not assign public IP addresses on launch. Each one gets a
default route through a NAT gateway, and receives generated subnet and route
table names.

If there are fewer NAT gateways than private subnets, the `index % number of
gateways` calculation shares them in rotation. For example, two NAT gateways
can serve four private subnets, alternating between the two gateways.

### `_add_nat_gateways()`

Creates the effective number of NAT gateways, placing each in a different
public subnet. For every NAT gateway it also creates an Elastic IP and assigns
that address to the gateway. It returns the gateway IDs so private subnet
routes can refer to them.

### `_group(subnet_name, subnet_type)`

Prevents duplicated words in generated names. If the configured name for a tier
is already the same as its type (for example, the name is `public` and the type
is `public`), it returns an empty extra group name. Otherwise it returns the
custom name.

### `_name_subnet(...)`

Adds resource-specific `Name` tags to a subnet and its route table. The name is
built from the common account/region prefix, network name, optional tier group,
subnet type, and position (`primary`, `secondary`, and so on).

The route-table lookup uses the child ID that the CDK subnet construct creates.

### `_name_gateways()`

Adds `Name` tags to the Internet Gateway and to each NAT gateway and its
Elastic IP. It uses the same account/region naming convention as the subnets.
NAT gateway and Elastic IP names also include their position.

## `stacks/network_stack.py` - placing the network in a stack

This module wraps the `Network` construct in an AWS CDK stack. A stack is a
deployable group of AWS resources.

### Imports

- `aws_cdk` provides stack and output types.
- `Construct` is the base type for CDK constructs.
- `Environment` provides the selected account, region, common settings, and
  network settings.
- `Network` creates the actual network resources.

### `NetworkStack.__init__(...)`

The constructor first initializes the CDK stack. It then:

1. Gives CDK the availability-zone names for the selected account and region.
2. Applies every shared tag from `environment.common.tags` to the stack's
   resources.
3. Creates the `Network` construct using the environment's shared and network
   settings.
4. Publishes the VPC ID and comma-separated public subnet IDs as CloudFormation
   outputs.
5. Publishes private subnet IDs only when private subnets are configured.

Other stacks or deployment steps can use these outputs to find the network
resources after deployment.

### `_output(logical_id, value, export_suffix)`

Creates a CloudFormation output and gives it an export name based on the stack
name plus a suffix. The suffix distinguishes the VPC ID, public subnet IDs, and
private subnet IDs. Exports let other CloudFormation stacks refer to these
values.

## `constructs/security.py` - security group settings and AWS resources

Like the network module, this has two parts: the settings read from YAML, and
the construct that builds the resources.

### `RuleProps`

One ingress or egress rule: a `peer`, a `description`, a `protocol` (`tcp`,
`udp`, `icmp` or `all`) and `ports`, which is a single number or a range such
as `30000-32767`. `icmp` and `all` cover every port, so they take no `ports`.

`__post_init__` rejects an unknown protocol, a missing or impossible port, and
a malformed CIDR, so a mistake is reported when the config is read rather than
part way through a deployment. `references_group` tells a group name apart from
a CIDR, an `sg-` id or a `pl-` prefix list id, and `port()` converts the
protocol and ports into the CDK `Port` object.

### `SecurityGroupProps`

One group: its `name`, `description`, `ingress` and `egress` rules, optional
extra `tags`, and `allow_all_outbound`. Outbound is open unless `egress` is
listed, in which case those rules are the only way out; setting
`allow_all_outbound: false` with no egress rules closes outbound entirely.
Listing egress rules *and* `allow_all_outbound: true` is rejected, because
CloudFormation would silently ignore the rules.

`load_all` parses the whole `security_groups:` list and checks it holds
together: no duplicate group names, no duplicate rules within a group, and
every group a rule points at actually exists.

### `SecurityGroups`

Creates every group first and only then adds the rules, so groups can reference
each other in any order, including mutually. A rule whose peer is another group
in the same config becomes its own `AWS::EC2::SecurityGroupIngress` resource
rather than an inline rule, which is what lets a cluster group and a node group
point at each other without the deployment deadlocking. Rules that name a CIDR
or an existing `sg-` id are written inline on the group.

`__getitem__` returns a group by its configured name, so a later stack can
write `security_groups["eks-nodes"]`.

## `stacks/security_stack.py` - placing the groups in a stack

Takes the `Environment` and the VPC the groups belong to, applies the shared
tags, builds the `SecurityGroups` construct, and exports every group ID as
`{stack_name}-{group name}-sg-id` so another CloudFormation stack can import it.

Because the VPC arrives as an object from `NetworkStack`, CDK records a
dependency between the two stacks and turns the VPC ID into an export/import
pair. The network stack is therefore always deployed first, and cannot be
destroyed while the security stack still refers to it.

### Security groups for EKS

The `security_groups:` list in `deployments/<env>/env.yaml` defines groups for
the cluster and surrounding resources. A rule's `source` (ingress) or
`destination` (egress) can be another configured group name, a CIDR, an existing
`sg-` ID, or a `pl-` prefix list ID.

Keep these three roles separate:

| Group | Created by | Intended attachment |
| ----- | ---------- | ------------------- |
| `eks-cluster-sg-<cluster>-<id>` | EKS automatically | Control plane ENIs and, normally, managed node ENIs |
| `eks-cluster` | This security stack | Additional group on control plane ENIs |
| `eks-nodes` | This security stack | Node ENIs, through the node group's launch template |

EKS creates its own cluster security group with self-referencing inbound rules
and unrestricted outbound traffic by default. On cluster updates, EKS restores
its self-referencing rules and required tags if removed. The unrestricted
outbound rule can be restricted, provided the required cluster traffic remains
allowed; EKS does not restore every rule indiscriminately.

The configured `eks-cluster` is already the additional group for custom
control-plane rules. Pass its ID to `resourcesVpcConfig.securityGroupIds` when
creating the cluster. It supplements the EKS-created group rather than
replacing it, and EKS does not automatically attach it to nodes. Creating a
second custom control-plane group is unnecessary just to add more rules.

Put node-level rules on `eks-nodes` and attach that group through the node
launch template. If the launch template specifies custom security groups, EKS
does not automatically add its cluster security group to those nodes, so the
custom rules must allow the required node/control-plane communication.

Creating these groups does not attach them to a cluster or its nodes: that
wiring belongs in the cluster and node-group definitions. Once `eks-cluster`
is attached to the control plane ENIs, `source: eks-cluster` can match traffic
from those interfaces. The EKS-created group's ID is only available after the
cluster exists, so it cannot be a pre-creation reference in this config.

See [the README's EKS security group section](../../README.md#security-groups-and-eks)
and [AWS's security group requirements](https://docs.aws.amazon.com/eks/latest/userguide/sec-group-reqs.html).

## `constructs/iam.py` - service roles and instance profiles

`IamRoleProps` describes a role's config name, optional description, and exactly
one trust format: a service shorthand (`assumed_by`) or a full `trust_policy`.
It accepts AWS-managed policy names (`managed_policies`), existing managed-policy
ARNs (`managed_policy_arns`) and named permission documents (`custom_policies`).
`trust_policy` and each `custom_policies` value may also be a relative YAML file
path instead of an embedded mapping.
`InstanceProfileProps` associates a profile name with exactly one role from the
same configuration; profiles do not have their own policies.

`IamProps.from_dict` loads the optional `iam.roles` and `iam.instance_profiles`
lists. It rejects unknown fields, malformed values, duplicate names (including
case-only duplicates), duplicate policies and unresolved profile roles. A role
used in an instance profile must explicitly allow EC2 to call `sts:AssumeRole`.
The check accepts service trust in either format, including list-valued
principals/actions. It does not evaluate IAM conditions or deny precedence.

Policy parsing validates IAM document structure and JSON-compatible values,
rejects ambiguous trust configuration, and distinguishes trust policies
(principals, no resources) from identity permission policies (resources, no
principals). AWS remains responsible for evaluating permissions and validating
service-specific actions and conditions.

`load_environment` passes the repository root (the parent of `deployments/`) as `base_dir`
through `IamProps.from_dict` and `IamRoleProps.from_dict`. `_policy_document`
resolves string values against that directory, reads UTF-8 text and uses
`yaml.safe_load` before applying the same document validation. File contents
must be policy mappings, not references to further files. Absolute paths and
URLs are rejected; direct parser callers must supply `base_dir` when using files
rather than relying on the process working directory. Errors identify the
role, custom policy name where applicable, and file.

The parsed props contain the resolved documents, so the construct does not
need separate file-handling branches. Moving a policy between embedded YAML
and a file preserves the synthesized template, including role logical IDs,
profile references, exports, conditions and permissions.

`Iam` creates the roles first and then the profiles. A profile's `Roles` property
references its role, so CloudFormation creates that role before the profile.
The `roles` and `instance_profiles` dictionaries expose resources by config
name for downstream stacks. Full names use
`common.name(env_name, name, "role")` or `"profile"`; the environment prevents
different deployments from colliding in account-wide IAM. Names are checked
against IAM's character and length limits before resources are built.

Service shorthand roles keep the original L2 `iam.Role`. Full trust documents
use `iam.CfnRole`, preserving all statements, conditions, version and metadata
without adding a placeholder principal. That resource is placed at the same
`<name>Role/Resource` construct path as the L2 resource, so changing trust format
does not change its logical ID. A mutable `iam.IRole` facade exposes the created
role to downstream CDK code, with its ARN token retaining the resource dependency.
Both formats preserve inline documents through the L1 `Policies` property;
the documents are not re-encoded through CDK's L2 `PolicyDocument`.
Output logical IDs also stay stable across trust format changes.

Managed-policy ARNs attach existing policies rather than creating them.
Custom policies are embedded in the role as inline IAM policies. They can be combined with AWS-managed
policies without changing trust. See the
[custom policy and trust examples](../../README.md#custom-permissions-and-trust).

## `stacks/iam_stack.py` - deploying roles and profiles

`IamStack` applies the common tags, creates `Iam`, and exports each role and
profile's name and ARN. No VPC is needed, so it is independent of the network
and security stacks.

Dev loads the EKS control-plane trust from
[`policies/eks-cluster-trust.yaml`](../../policies/eks-cluster-trust.yaml).
It defines an EKS control-plane role trusted by `eks.amazonaws.com`, a worker
role trusted by `ec2.amazonaws.com`, and a worker instance profile. A future
cluster uses the control-plane role; managed node groups take the node role
ARN and manage their own profiles. Self-managed EC2 workers use the explicit
instance profile. This stack does not create or attach to a cluster or nodes.

The dev worker role includes IPv4 VPC CNI permissions until a dedicated IRSA or
Pod Identity role is configured for the add-on. See the
[IAM configuration reference](../../README.md#iam) for policy names and usage.

## `__init__.py` files - package markers

The following files are currently empty:

- `cognitech_cdk/__init__.py`
- `cognitech_cdk/common/__init__.py`
- `cognitech_cdk/constructs/__init__.py`
- `cognitech_cdk/stacks/__init__.py`

They mark these directories as Python packages so modules can be imported using
names such as `cognitech_cdk.common.config` and
`cognitech_cdk.stacks.network_stack`. They do not currently define extra
functions or behavior.

## How the pieces run together

When the CDK app is run, the code outside this package (`app.py`) chooses an
environment name, loads its `deployments/<environment>/env.yaml`, and passes
the resulting `Environment` object into `NetworkStack`. The stack builds the
network construct, adds tags and outputs, and CDK synthesizes the result.

When the environment also declares `security_groups:`, `app.py` creates a
`SecurityStack` as well and hands it the VPC from the network stack. CDK sees
that reference and deploys the network stack first. An environment with no
`security_groups:` gets no security stack, because CloudFormation rejects a
template with no resources.

When `iam.roles` is nonempty, `app.py` also creates `IamStack`. Without that
section, no IAM stack is synthesized; the existing prod deployment is unchanged.

In short: YAML supplies the choices, `Common` and `NetworkProps` check and
organize them, `Network` describes the AWS resources, and `NetworkStack` makes
those resources deployable and discoverable.
