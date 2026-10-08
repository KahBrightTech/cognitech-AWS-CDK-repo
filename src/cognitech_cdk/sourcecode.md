# Source Code Guide

This guide explains the Python code in the `cognitech_cdk` package in everyday
language. The package uses AWS CDK (the Cloud Development Kit) to describe AWS
network resources as code. CDK turns that description into a CloudFormation
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
        |                    constructs/network.py (NetworkProps)
        v
stacks/network_stack.py ---> constructs/network.py (Network)
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
network `NetworkProps`.

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

In short: YAML supplies the choices, `Common` and `NetworkProps` check and
organize them, `Network` describes the AWS resources, and `NetworkStack` makes
those resources deployable and discoverable.
