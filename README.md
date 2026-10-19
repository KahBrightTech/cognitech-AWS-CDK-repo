# cognitech-AWS-CDK-repo

AWS infrastructure built with the AWS CDK (Python).

Resources follow the same naming convention and common-tag model as the
[Terraform modules](../Cognitech-terraform-iac-modules), so both estates look
the same in the console.

**What it builds today:** one VPC per environment with an internet gateway and
public subnets, and optionally private subnets with NAT gateways and Elastic IPs.
You choose how many availability zones.

---

## Quick start

Run these once, from the repo root, to get a working toolchain:

```bash
# WSL / Linux
python3 -m venv .venv-linux
source .venv-linux/bin/activate
pip install -r requirements-dev.txt
npm install -g aws-cdk@2

pytest -q
cdk ls --context env=dev
```

### What each command does

| Command                             | What it does                                                                 |
| ----------------------------------- | ---------------------------------------------------------------------------- |
| `python3 -m venv .venv-linux`       | Creates an isolated Python environment in `.venv-linux/`. Run once.          |
| `source .venv-linux/bin/activate`   | Activates it for the current shell. **Required before every `cdk` command.** |
| `pip install -r requirements-dev.txt` | Installs `aws-cdk-lib`, `constructs`, `PyYAML` and `pytest` into the venv.  |
| `npm install -g aws-cdk@2`          | Installs the `cdk` command-line tool. Separate from the Python library.      |
| `pytest -q`                         | Runs the unit tests against the synthesized CloudFormation template.         |
| `cdk ls --context env=dev`          | Lists the stacks for the `dev` environment. Proves the config parses.        |

**`python3 -m venv .venv-linux`** builds the environment in a folder called
`.venv-linux`. The name is arbitrary but it is the one in `.gitignore` and in
`.vscode/settings.json`, so keep it. A venv built on Windows cannot be used from
WSL and vice versa — that is why there is a separate `.venv-win` for the editor.

**`source .venv-linux/bin/activate`** is the step people miss. `cdk.json` runs the
app with `python app.py`, and Ubuntu only ships `python3`, so without an activated
venv the CLI fails with:

```
/bin/sh: 1: python: not found
Subprocess exited with error 127
```

Your prompt should start with `(.venv-linux)` once it is active. Check with:

```bash
which python && python --version      # must resolve inside .venv-linux
```

A prefix like `(admin-mdpp)` is an AWS profile, not a virtual environment.
Deactivate with `deactivate`. You need to re-activate in every new terminal.

**`pip install -r requirements-dev.txt`** pulls in `requirements.txt` (the runtime
libraries) plus `pytest`. Use `requirements.txt` alone if you only want to deploy
and not run tests. Re-run it whenever either file changes.

**`npm install -g aws-cdk@2`** installs the CLI that provides `cdk ls`, `synth`,
`diff`, `deploy` and `bootstrap`. It is versioned independently of the
`aws-cdk-lib` pinned in `requirements.txt`; re-run it when the CLI reports a newer
version. Node 20 still works but is deprecated — the CLI warns that the AWS SDK
will require Node 22+ from January 2027, so upgrade when convenient:

```bash
nvm install 22 && nvm use 22 && npm install -g aws-cdk@2
```

**`pytest -q`** synthesizes each stack in memory and asserts against the resulting
template — no AWS account or credentials needed. 37 tests cover naming, tagging,
AZ counts and the NAT-gateway rules.

**`cdk ls --context env=dev`** reads `deployments/dev/env.yaml` and prints the
stack names it would create. It is the fastest way to confirm a config change is
valid, and it also needs no credentials:

```
int-production-use1-dev-network
```

---

## Layout

```
app.py                                  entrypoint: picks an environment, builds the stacks
cdk.json
pyproject.toml                          pytest config (puts src/ on the path)

src/cognitech_cdk/
  common/
    config.py                           Common: naming + tags, mirrors the Terraform object
    environment.py                      reads deployments/<env>/env.yaml
    schema.py                           shared YAML validators
    documents.py                        repo-relative YAML/JSON files and [[placeholders]]
  constructs/
    network.py                          Network: VPC, IGW, subnets, NAT gateways, EIPs
    security.py                         SecurityGroups: groups and the rules between them
    iam.py                              Iam: roles, policies, users and EC2 instance profiles
    lookups.py                          deploy-time lookups (hosted zones, SSO roles)
    <family>.py                         one module per Tenant-account family (see below)
  stacks/
    base.py                             EnvironmentStack: tags, names and exports
    network_stack.py                    NetworkStack: tags, construct, outputs
    security_stack.py                   SecurityStack: security groups, one export per group
    iam_stack.py                        IamStack: roles, profiles, names and ARN exports
    <family>_stack.py                   one stack per Tenant-account family (see below)

deployments/
  dev/env.yaml                          one folder per environment
  prod/env.yaml

tests/unit/
.github/workflows/
  deploy-primary-mdpp-dev.yml           deploy dev into md-preproduction
  pr.yml                                tests + synth on every PR
  sync-stack-choices.yml                regenerates the stack dropdown from `cdk list`
scripts/create_github_oidc_roles.sh     one-time AWS setup for the pipeline
scripts/sync_stack_choices.py           writes that dropdown into the deploy workflows
```

**Constructs** are reusable building blocks. **Stacks** group constructs into one
deployable CloudFormation stack. A construct is not deployable on its own.

### Resources you did not ask for

A deployed stack contains a few things that are not in `env.yaml`:

| Resource                        | Where it comes from                                       |
| ------------------------------- | --------------------------------------------------------- |
| `Custom::VpcRestrictDefaultSG`  | the `restrictDefaultSecurityGroup` feature flag — see below |
| `AWS::Lambda::Function`         | the handler backing that custom resource                   |
| `AWS::IAM::Role`                | the execution role for that Lambda                         |
| `AWS::CDK::Metadata`            | CDK's version marker, added to every stack                 |

**`Custom::VpcRestrictDefaultSG`.** AWS creates a *default security group* with
every VPC. You do not declare it, CloudFormation will not delete it, and out of
the box it allows all traffic between its members plus all outbound.

The flag in `cdk.json` makes CDK strip every ingress and egress rule from it at
deploy time:

```json
"@aws-cdk/aws-ec2:restrictDefaultSecurityGroup": true
```

That is a compliance control — an open default security group is flagged by CIS
AWS Foundations 5.3, the AWS Config rule `vpc-default-security-group-closed`, and
most PCI/HIPAA audits. The Lambda and its role exist only to make that one API
call; they run at create/update/delete and cost nothing at rest.

The practical effect is that anything launched **without** an explicit security
group gets no connectivity, which is the point — it fails loudly instead of
silently inheriting an open group.

To turn it off, set the flag to `false` and redeploy; the three resources go
away. Note that CDK does **not** restore the original rules, so the default
security group stays closed and you would re-open it by hand.

---

## Configuring an environment

One file per environment: `deployments/<env>/env.yaml`.

```yaml
account_id: "111122223333"
region: us-east-1

common:
  account_name: int-production
  region_prefix: use1
  global: false
  account_name_abr: intprod     # short form for ALBs/target groups (32-char cap)
  environment_abr: dev
  tags:
    Build-method: aws-cdk
    Environment: development
    ManagedBy: aws-cdk/cognitech-AWS-CDK-repo
    Owner: kbrigthain@gmail.com
    Compliance: hippaa

network:
  name: dev
  cidr_block: 10.20.0.0/16
  azs: 2
  private_subnets: false

security_groups:             # optional, see below
- name: eks-nodes
  description: EKS worker nodes
  ingress:
  - description: Node to node
    source: eks-nodes
    protocol: all
```

### `common`

Mirrors the `common` variable in the Terraform modules:

| Key                  | Required | Purpose                                                 |
| -------------------- | -------- | ------------------------------------------------------- |
| `account_name`     | yes      | First segment of every resource name                    |
| `region_prefix`    | yes      | Second segment, e.g.`use1`                            |
| `tags`             | yes      | Applied to every resource in the stack                  |
| `global`           | no       | Marks global (non-regional) resources; default`false` |
| `account_name_abr` | no       | Short account name for length-capped resources          |
| `environment_abr`  | no       | Short environment name, available to future constructs  |

`tags` must include `Build-method`, `Environment`, `ManagedBy`, `Owner` and
`Compliance`. `Name` is rejected there — it is built per resource.

### `network`

| Key                      | Required | Default   | Purpose                                          |
| ------------------------ | -------- | --------- | ------------------------------------------------ |
| `name`                   | yes      | —         | VPC name segment, e.g. `dev`                     |
| `cidr_block`             | yes      | —         | VPC range                                        |
| `azs`                    | no       | `2`       | **1 to 4** availability zones                    |
| `private_subnets`        | no       | `false`   | Add private subnets + NAT gateways + Elastic IPs |
| `nat_gateways`           | no       | one per AZ | Only used when `private_subnets: true`          |
| `public_subnet_cidrs`    | no       | carved    | Exact public ranges, one per AZ, in order        |
| `private_subnet_cidrs`   | no       | carved    | Exact private ranges, one per AZ, in order       |
| `cidr_mask`              | no       | `24`      | Prefix length used when a tier has no CIDRs      |
| `public_subnet_name`     | no       | `public`  | Name segment for the public tier, e.g. `edge`    |
| `private_subnet_name`    | no       | `private` | Name segment for the private tier, e.g. `app`    |

**Public subnets only** — no NAT gateways, no Elastic IPs, nothing billed hourly:

```yaml
network:
  name: dev
  cidr_block: 10.20.0.0/16
  azs: 2
  private_subnets: false
```

**With private subnets** — adds a private subnet per AZ plus NAT gateways:

```yaml
network:
  name: prd
  cidr_block: 10.30.0.0/16
  azs: 3
  private_subnets: true
  nat_gateways: 1      # omit for one per AZ
```

`private_subnets: true` with `nat_gateways: 0` is rejected: those subnets would
have no route out.

The network stack exports the VPC ID as `{stack_name}-vpc-id`, each subnet tier
as a comma-separated list, and each subnet by AZ position:

| Output | Export name |
| ------ | ----------- |
| `VpcId` | `{stack_name}-vpc-id` |
| `PublicSubnetIds` | `{stack_name}-public-subnet-ids` |
| `PublicSubnetPrimaryId` | `{stack_name}-public-subnet-primary-id` |
| `PrivateSubnetIds` | `{stack_name}-private-subnet-ids` |
| `PrivateSubnetPrimaryId` | `{stack_name}-private-subnet-primary-id` |

Additional subnets use `secondary`, `tertiary` and `quaternary`. Private
outputs exist only when `private_subnets: true`.

### Subnet CIDR ranges

You can either name every range, or let them be carved from `cidr_block`.

**Explicit** — one CIDR per AZ, in the same order as the zones. This is the
equivalent of the Terraform module's `primary_cidr_block`, `secondary_cidr_block`
and so on:

```yaml
network:
  name: prd
  cidr_block: 10.30.0.0/16
  azs: 3
  private_subnets: true
  public_subnet_cidrs:
    - 10.30.0.0/20      # us-east-1a, "primary"
    - 10.30.16.0/20     # us-east-1b, "secondary"
    - 10.30.32.0/20     # us-east-1c, "tertiary"
  private_subnet_cidrs:
    - 10.30.64.0/20
    - 10.30.80.0/20
    - 10.30.96.0/20
```

**Carved** — omit a tier and it takes the lowest free `/{cidr_mask}` blocks:

```yaml
network:
  name: dev
  cidr_block: 10.20.0.0/16
  azs: 2
  cidr_mask: 24        # default
```

→ `10.20.0.0/24`, `10.20.1.0/24` for public, then `10.20.2.0/24`, `10.20.3.0/24`
for private if it is enabled.

**Mixed** — you can pin one tier and carve the other. Explicit ranges are
reserved first, so a carved tier never collides with them:

```yaml
  public_subnet_cidrs: [10.20.0.0/24, 10.20.1.0/24]
  # private omitted -> gets 10.20.2.0/24 and 10.20.3.0/24
```

The config is validated at synth time, so mistakes fail before a deploy starts:

| Mistake                                   | Error                                      |
| ----------------------------------------- | ------------------------------------------ |
| Fewer or more CIDRs than `azs`            | `lists 2 CIDRs but azs is 3`               |
| A range outside `cidr_block`              | `subnet CIDR 10.99.0.0/24 is outside the VPC range` |
| Two ranges that overlap                   | `subnet CIDRs ... and ... overlap`         |
| `cidr_mask` too large for the VPC         | `no free /26 block left in 10.20.0.0/24`   |

### Availability zones

`azs` is 1 to 4, derived from `region` and named by position:

| `azs` | Zones                                     | Subnets (public only) | Subnets (with private) |
| ------- | ----------------------------------------- | --------------------- | ---------------------- |
| 1       | `us-east-1a`                            | 1                     | 2                      |
| 2       | `us-east-1a`, `-1b`                   | 2                     | 4                      |
| 3       | `us-east-1a`, `-1b`, `-1c`          | 3                     | 6                      |
| 4       | `us-east-1a`, `-1b`, `-1c`, `-1d` | 4                     | 8                      |

Positions use the Terraform words: `primary`, `secondary`, `tertiary`,
`quaternary`. Zones are written into the template rather than looked up, so
`cdk synth` and the tests need no AWS credentials.

Raising `azs` adds subnets; lowering it **deletes** them. Run `cdk diff` first.

### `security_groups`

Optional. A list of security groups to build for whatever runs in the VPC — a
cluster, a load balancer, a database. Each entry becomes one security group in
its own `SecurityStack`; an environment that lists none gets no such stack.

| Key                   | Required | Default        | Purpose                                     |
| --------------------- | -------- | -------------- | ------------------------------------------- |
| `name`                | yes      | —              | Name segment, e.g. `eks-nodes`              |
| `description`         | no       | generated      | Shown in the console; cannot be changed later |
| `ingress`             | no       | none           | Inbound rules                               |
| `egress`              | no       | allow all out  | Outbound rules; listing any closes the rest |
| `allow_all_outbound`  | no       | see below      | `false` with no `egress` blocks all outbound |
| `tags`                | no       | none           | Extra tags, e.g. `kubernetes.io/cluster/dev` |

Each rule takes:

| Key           | Required | Default   | Purpose                                              |
| ------------- | -------- | --------- | ---------------------------------------------------- |
| `source`      | ingress  | —         | Where the traffic comes from                         |
| `destination` | egress   | —         | Where the traffic goes                               |
| `description` | no       | generated | Shown against the rule in the console                |
| `protocol`    | no       | `tcp`     | `tcp`, `udp`, `icmp` or `all`                        |
| `ports`       | tcp/udp  | —         | `443`, or a range like `30000-32767`                 |

`source` and `destination` are one of:

| Value            | Meaning                                       |
| ---------------- | --------------------------------------------- |
| `eks-nodes`      | another group in this list, or the group itself |
| `10.20.0.0/16`   | an IPv4 range                                 |
| `2600:1f18::/32` | an IPv6 range                                 |
| `sg-0abc123`     | a group that already exists outside this stack |
| `pl-0abc123`     | a prefix list, e.g. an AWS service range      |

A cluster and its nodes referring to each other is the normal case and is safe:
rules between two groups are built as separate resources, so neither has to
exist before the other.

```yaml
security_groups:
- name: eks-cluster
  description: EKS control plane
  ingress:
  - description: API server from the nodes
    source: eks-nodes
    ports: 443

- name: eks-nodes
  description: EKS worker nodes
  tags:
    kubernetes.io/cluster/dev: owned
  ingress:
  - description: Node to node, all ports
    source: eks-nodes          # a group may reference itself
    protocol: all
  - description: Control plane to kubelet
    source: eks-cluster
    ports: 10250
  - description: Ingress controller to the NodePort range
    source: alb
    ports: 30000-32767

- name: alb
  description: Public load balancer in front of the cluster
  ingress:
  - description: HTTPS from anywhere
    source: 0.0.0.0/0
    ports: 443
```

Outbound is open unless `egress` is listed, in which case those rules are the
only way out:

```yaml
- name: database
  description: Postgres, no route to the internet
  ingress:
  - description: Postgres from the nodes
    source: eks-nodes
    ports: 5432
  egress:
  - description: Replication to the secondary
    destination: 10.20.0.0/16
    ports: 5432
```

Mistakes are caught when the config is read, before anything is built: a group
name that is not defined, a repeated group or rule, a port outside 0–65535, a
backwards range, a malformed CIDR, and `allow_all_outbound: true` alongside
`egress` rules that AWS would then ignore.

Each group's ID is exported as `{stack_name}-{name}-sg-id`, and in code a group
is reached by its configured name:

```python
stack.security_groups["eks-nodes"]          # ec2.SecurityGroup
```

The groups live in their own stack, which takes the VPC from the network stack.
CDK turns that into a CloudFormation export/import, so the ordering is enforced
by AWS rather than by you remembering it: the network stack is deployed first,
and it cannot be destroyed while the security stack still imports its VPC.

#### Security groups and EKS

A cluster uses these three group roles. Only two of them are built here;
creating the groups does not attach them to a cluster or its nodes.

| Group                            | Created by          | Attached to                        |
| -------------------------------- | ------------------- | ---------------------------------- |
| `eks-cluster-sg-<cluster>-<id>`  | EKS automatically   | control plane ENIs and, normally, managed node ENIs |
| `eks-cluster` (this config)      | this stack          | control plane ENIs, when passed to the cluster |
| `eks-nodes` (this config)        | this stack          | nodes, when configured in the launch template |

EKS creates its own cluster security group on every cluster and there is no way
to turn that off. By default it allows all traffic from itself and all traffic
outbound. On cluster updates, EKS restores its self-referencing rules and
required tags if removed. You can restrict unrestricted outbound traffic while
preserving required cluster communication; EKS does not restore every rule.

The `eks-cluster` group here is the *additional* group to pass to the cluster as
`resourcesVpcConfig.securityGroupIds`. AWS adds it to the group above rather
than replacing it, and only on the control plane ENIs. Add custom control-plane
rules to this group; a second custom group is unnecessary just to add rules.
Node-level rules belong on `eks-nodes`. If the node launch template specifies
custom security groups, EKS does not automatically attach its cluster security
group to those nodes, so the custom rules must permit the required traffic.

Security group permissions are additive: a restrictive additional group does
not override traffic another attached group allows.

Once `eks-cluster` is attached to the control plane ENIs, rules can use
`source: eks-cluster` to match traffic from those interfaces. The EKS-created
group's ID is only available after cluster creation, so it cannot be a
pre-creation reference in this config.

See [the source documentation](src/cognitech_cdk/sourcecode.md#security-groups-for-eks)
and [AWS's security group requirements](https://docs.aws.amazon.com/eks/latest/userguide/sec-group-reqs.html).

---

### `iam`

Optional. Configures a separate IAM stack for service roles and EC2 instance
profiles. Environments with no roles get no IAM stack; prod is unchanged.

```yaml
iam:
  roles:
  - name: eks-cluster
    description: EKS control plane service role
    assumed_by: eks.amazonaws.com
    managed_policies:
    - AmazonEKSClusterPolicy
  - name: eks-nodes
    description: EKS EC2 worker node role
    assumed_by: ec2.amazonaws.com
    managed_policies:
    - AmazonEKSWorkerNodePolicy
    - AmazonEC2ContainerRegistryPullOnly
    - AmazonEKS_CNI_Policy
  instance_profiles:
  - name: eks-nodes
    role: eks-nodes
```

| Section | Key | Purpose |
| ------- | --- | ------- |
| `roles` | `name` | Required config key; letters/digits, hyphens and underscores |
| `roles` | `assumed_by` | AWS service principal shorthand; use exactly one of this or `trust_policy` |
| `roles` | `trust_policy` | Full IAM trust document or a relative YAML file path |
| `roles` | `description` | Optional description |
| `roles` | `managed_policies` | Optional list of AWS-managed policy names, not ARNs; paths such as `service-role/...` are supported |
| `roles` | `managed_policy_arns` | Optional list of existing managed-policy ARNs, including customer-managed policies |
| `roles` | `custom_policies` | Optional mapping of policy names to IAM permission documents or relative YAML file paths |
| `instance_profiles` | `name` | Required profile config key |
| `instance_profiles` | `role` | Required exact name of an EC2-trusted role in the same `roles` list |

With `assumed_by`, trust is limited to the configured service. With
`trust_policy`, trust is exactly the document supplied; the two fields cannot
be combined, and no extra principal is added. No administrator policies are
attached by default. Unknown fields, malformed entries, duplicate names or
policies, unknown profile roles, and profiles without an explicit EC2
`Allow` / `sts:AssumeRole` service trust statement are rejected.
IAM names include the environment because IAM is account-wide, and complete
names are checked against the 64-character role and 128-character profile limits.

#### Custom permissions and trust

Policies and trust belong to the **role**, never to the instance profile.
An instance profile only carries one EC2-trusted role. All three permission
fields can be combined: `managed_policies` imports AWS-managed policies by
name, `managed_policy_arns` attaches existing policies by ARN, and
`custom_policies` creates named inline policies embedded in the role.

For example, this role uses explicit EC2 trust, attaches a customer-managed
policy and adds an inline S3 permission. Replace the example policy ARN and
bucket with your actual resources before deploying:

```yaml
iam:
  roles:
  - name: app-worker
    trust_policy:
      Version: "2012-10-17"
      Statement:
      - Effect: Allow
        Principal:
          Service: ec2.amazonaws.com
        Action: sts:AssumeRole
    managed_policies:
    - AmazonEKSWorkerNodePolicy
    managed_policy_arns:
    - arn:aws:iam::111122223333:policy/team/CustomWorkerPolicy
    custom_policies:
      ReadAppConfig:
        Version: "2012-10-17"
        Statement:
        - Sid: ReadConfig
          Effect: Allow
          Action:
          - s3:GetObject
          Resource: arn:aws:s3:::example-config-bucket/config/*
          Condition:
            Bool:
              aws:SecureTransport: "true"
  instance_profiles:
  - name: app-worker
    role: app-worker
```

The ARN attachment does **not** create that managed policy; it must already
exist. Inline permissions do not change who can assume the role.

##### Policies in separate YAML files

`trust_policy` and each value in `custom_policies` accept either an embedded
document or a string naming a YAML file. Paths are relative to the repository
root, not the directory containing `env.yaml` or the terminal's working directory. No extra fields
or YAML tags are needed:

```yaml
iam:
  roles:
  - name: app-worker
    trust_policy: policies/ec2-trust.yaml
    custom_policies:
      ReadAppConfig: policies/read-app-config.yaml
  instance_profiles:
  - name: app-worker
    role: app-worker
```

For that example, place the policies in the repository-root `policies/` folder:

```text
deployments/
  dev/
    env.yaml
policies/
  ec2-trust.yaml
  read-app-config.yaml
```

Contents of `policies/ec2-trust.yaml`:

```yaml
Version: "2012-10-17"
Statement:
- Effect: Allow
  Principal:
    Service: ec2.amazonaws.com
  Action: sts:AssumeRole
```

Contents of `policies/read-app-config.yaml` (replace the bucket before use):

```yaml
Version: "2012-10-17"
Statement:
- Effect: Allow
  Action: s3:GetObject
  Resource: arn:aws:s3:::example-config-bucket/config/*
  Condition:
    Bool:
      aws:SecureTransport: "true"
```

Files contain a complete IAM policy document, not an `iam:` or role wrapper.
They are loaded with safe YAML parsing, then validated exactly like embedded
documents. Mixing embedded and file-based permission policies is supported.
`assumed_by` and `trust_policy` remain mutually exclusive, even when trust
comes from a file. File-based permission documents still become **inline IAM
policies** on the role; they do not create customer-managed policies.

The repository includes [a reference permission policy](policies/example-read-config.yaml)
that allows `s3:GetObject` on `example-config-bucket/config/*` over HTTPS.
Dev attaches it to the `eks-nodes` role as `TestReadConfig`. The bucket name is
still an example; replace it in the file to target your resources. The reference is:

```yaml
custom_policies:
  TestReadConfig: policies/example-read-config.yaml
```

For VS Code YAML validation, root-level policy files can start with
`# yaml-language-server: $schema=./iam-policy.schema.json` to use the local
[IAM document schema](policies/iam-policy.schema.json). This overrides
SchemaStore's unrelated Aerleon schema for `policies/**/*.yaml`, which otherwise
reports errors such as `Missing property "terms"`. The directive is a YAML
comment and does not change the policy loaded by CDK; IAM-specific validation
still runs when loading the environment.

Missing/unreadable files, malformed YAML and invalid policy documents fail
configuration loading with the role, policy name (for permissions) and file
path in the error. Only local relative paths are accepted, not absolute paths
or URLs. File contents cannot recursively include other policy files.

Commit the referenced files with the configuration so CI can read them.
The existing dev workflow watches both `deployments/dev/` and root-level
`policies/`. If you keep shared policies elsewhere, add their paths to
that workflow's push filters too.

For non-EC2 roles, a custom trust document can instead use AWS account/role
principals, federated principals, multiple statements and conditions. For
example, an automation role can use:

```yaml
trust_policy:
  Version: "2012-10-17"
  Statement:
  - Effect: Allow
    Principal:
      AWS: arn:aws:iam::111122223333:role/trusted-automation
    Action: sts:AssumeRole
    Condition:
      StringEquals:
        sts:ExternalId: example-external-id
```

Use this **instead of** `assumed_by`; it is not suitable for an EC2 instance
profile unless the document also explicitly trusts EC2. Use narrowly scoped
principals and conditions, not a wildcard trust principal.

Quote policy version dates so YAML treats them as strings. Documents use IAM
JSON field names (`Statement`, `Effect`, `Action`, `Principal`, `Resource`,
`Condition`); `Statement` can be a single mapping or a nonempty list. Trust
statements require a principal and cannot specify resources; inline permission
statements require resources and cannot specify principals. `Deny` and the
corresponding `Not*` fields are preserved, not silently converted to `Allow`.
Conditions and metadata are preserved. Local validation checks document
structure, not whether AWS accepts every action, condition or referenced
identity. Conditions and deny statements still govern whether EC2 can actually
assume a profile's role.

Dev loads `eks-cluster` trust from
[`policies/eks-cluster-trust.yaml`](policies/eks-cluster-trust.yaml)
and keeps shorthand trust on `eks-nodes`. The node role also attaches
`TestReadConfig` from the example permission-policy file above.
Switching trust formats preserves the role's
logical ID and name.

Dev creates:

| Resource | Name | Usage |
| -------- | ---- | ----- |
| Control-plane role | `md-preproduction-use1-dev-eks-cluster-role` | Pass to the EKS cluster's `role` / `roleArn` |
| Node role | `md-preproduction-use1-dev-eks-nodes-role` | Pass its ARN to a managed node group's `nodeRole` |
| Node instance profile | `md-preproduction-use1-dev-eks-nodes-profile` | Attach to self-managed EC2 worker instances |

For managed node groups, EKS manages the instance profile. Do not specify
`IamInstanceProfile` in their launch templates; supply the node role ARN instead.
The profile created here is for self-managed EC2 workers. Creating the roles and
profile does not create a cluster, node group or EC2 instance.

`AmazonEKS_CNI_Policy` is on the dev node role so the IPv4 VPC CNI can work
without a separate add-on identity. AWS recommends moving this policy to a
dedicated VPC CNI role using IRSA or EKS Pod Identity; remove it from the node
role after that identity is configured. These roles target conventional
EC2-backed EKS, not EKS Auto Mode or Fargate.

The IAM stack has no VPC dependency. A future cluster stack can receive both
the VPC and these roles, letting CDK establish the appropriate dependencies:

```python
iam_stack.iam.roles["eks-cluster"]             # iam.IRole
iam_stack.iam.roles["eks-nodes"]               # iam.IRole
iam_stack.iam.instance_profiles["eks-nodes"]   # iam.CfnInstanceProfile
```

Names and ARNs are exported as `{stack_name}-{name}-role-name`,
`{stack_name}-{name}-role-arn`, `{stack_name}-{name}-profile-name` and
`{stack_name}-{name}-profile-arn`.

Select `md-preproduction-use1-dev-iam` in the deploy workflow, or choose `all`.
Review the diff before deploying permission changes.

See AWS's [cluster IAM role](https://docs.aws.amazon.com/eks/latest/userguide/cluster-iam-role.html)
and [node IAM role](https://docs.aws.amazon.com/eks/latest/userguide/create-node-role.html) guidance.

### EKS compute configuration

Group worker capacity under `eks[].compute`. This is a YAML configuration block,
not a separate stack:

```yaml
eks:
- name: main
  role: eks-cluster
  subnet_tier: public
  compute:
    launch_templates:
    - name: general
      security_groups: [eks-nodes]
      volume_size: 20
      volume_type: gp3
    node_groups:
    - name: general
      role: eks-nodes
      launch_template: general
      subnet_tier: public
      instance_types: [t3.medium]
      min_size: 1
      desired_size: 1
      max_size: 2
      labels: {workload-type: system}
      taints:
      - {key: workload-type, value: system, effect: NO_SCHEDULE}
    karpenter:
      node_role: eks-nodes
      replicas: 1
      subnet_tier: public
      security_groups: [eks-nodes]
      values:
        nodeSelector: {workload-type: system}
        tolerations:
        - {key: workload-type, operator: Equal, value: system, effect: NoSchedule}
      node_pool:
        instance_categories: [t, m, c]
        capacity_types: [on-demand]
        cpu_limit: 8

eks_addons:
- cluster: main
  addons:
  - name: eks-pod-identity-agent
```

Karpenter still uses the existing `Karpenter` nested stack and requires the Pod
Identity agent in `eks_addons`. Its controller also needs schedulable worker
capacity. A node-group taint such as the example above needs matching tolerations
on workloads intended to use those nodes; Karpenter controller tolerations can be
set through `compute.karpenter.values` using the chart's settings.

Launch templates and managed node groups remain in the `NodeGroups` nested
stack. The node group's `launch_template` references a template's `name`;
alternatively, the existing `launch_template_id` / `launch_template_version`
settings select an externally managed template. If neither is supplied, EKS
generates its own launch template. Do not set `instance_type` in the template
and `instance_types` in the node group at the same time.

Existing `eks[].node_groups`, `eks[].launch_templates` and
`eks_addons[].karpenter` configurations remain supported. Moving them under
`compute` preserves resource scopes and logical IDs. Do not define the same
compute setting in both locations; conflicting definitions are rejected.
Fargate profiles are not implemented yet; unsupported compute keys fail validation.

The dev configuration includes commented compute examples and remains
control-plane-only until they are enabled. Resource settings and policy paths
in the example above assume matching IAM roles and security groups already exist.

#### System controllers versus application nodes

The commented dev examples reserve the managed node group for system workloads
using the label `workload-type: system` and taint
`workload-type=system:NoSchedule`. Controllers select that label and tolerate
the taint. The node-group API uses `NO_SCHEDULE`, while pod tolerations use
`NoSchedule`. A toleration permits a pod to run on those nodes; it does not
select them. The selector supplies that requirement.

| Placement | Components |
| --- | --- |
| Managed system nodes | CoreDNS, metrics-server, external-dns, cert-manager controller/CA injector/webhook, CSI controllers, CloudWatch operator/kube-state-metrics, Load Balancer Controller, Karpenter controller |
| Managed system nodes | Argo CD (non-HA example), NGINX ingress and Gateway Fabric control/data planes and certificate job, Kubecost services, AWX operator |
| Both eligible node types | VPC CNI, kube-proxy, Pod Identity agent, CSI node plugins, Secrets Store CSI driver/provider, CloudWatch/Fluent Bit and other enabled node-level collectors, Kubecost network costs/node-exporter if enabled |
| Karpenter nodes | Application pods, including AWX web/task/migration and its managed Postgres database |

Do not apply a system-only selector to a node-wide DaemonSet: application nodes
also need networking, identity, volume mounts, and monitoring. Retain existing
OS, hardware, and compute-type restrictions; "both node types" does not mean
installing every agent on incompatible nodes. The dev examples preserve native
DaemonSet defaults where appropriate and use broad taint tolerations on node
plugins that need them. Custom taints added later require another review.

Managed EKS add-ons take component-specific `configuration_values`; Helm charts
take chart-specific `values`. Toleration arrays replace, rather than append to,
defaults. The examples keep critical default tolerations where applicable.
Argo CD uses `global` placement for the non-HA installation; enabling Redis HA
requires separate Redis/HAProxy placement and adequate system-node capacity.
The AWX chart uses `operator-controller.spec.template.spec` for the operator,
but `AWX.spec.node_selector` and `postgres_selector` are YAML **strings** for
the operator-managed application pods.

Applications can target the existing Karpenter NodePool without a custom label:

```yaml
# In a Deployment's spec.template.spec:
nodeSelector:
  karpenter.sh/nodepool: default
```

Do not give applications the system toleration unless they intentionally need
system-node access. Argo CD placement affects its own controllers, not the
applications it deploys; those applications need their own scheduling settings.
Likewise, AWX execution pods created separately through Kubernetes container
groups require scheduling settings in their own pod specifications.

All dev compute and add-on scheduling examples remain commented out. Size the
system node group for the enabled controllers before rollout: the sample single
`t3.medium` is not a capacity guarantee for this full set of add-ons. Provision
system nodes and core networking/DNS/Pod Identity before expecting Karpenter
to provision application nodes. Adding a `NoSchedule` taint does not evict
already-running pods.

Scheduling fields were checked against AWS's Kubernetes 1.36 managed add-on
configuration schemas and the referenced chart versions. Native add-on schemas
are version-dependent; verify the chosen version with
`aws eks describe-addon-configuration` before enabling or upgrading.
The Terraform guide's general label/taint examples do not supply this full
controller/DaemonSet split; copying its older add-on versions or using one
blanket selector is not sufficient.

### Tenant-account stacks

The rest of the Terraform `formations/Tenant-account` formation is ported as one
config-driven stack per family. A stack is built only when its key is present in
`env.yaml`, so environments opt in section by section. Each construct module's
docstring has a complete YAML example and the validation rules.

| YAML key | Stack suffix | Terraform modules | Uses |
| --- | --- | --- | --- |
| `transit_gateway` | `transit-gateway` | Transit-gateway, -attachments, -route-table, -association, -propagation, -routes, -subnet-route | network |
| `s3_buckets` | `storage` | S3-Private-bucket | IAM roles (replication) |
| `key_pairs` | `key-pairs` | EC2-key-pair | |
| `secrets` | `secrets` | Secrets-manager | |
| `ssm` (`parameters`, `documents`) | `ssm` | SSM-Parameter-store, SSM-Documents | secrets, buckets |
| `certificates` | `certificates` | ACM-Public-Certs | |
| `backups` | `backup` | AWSBackup | |
| `load_balancing` | `load-balancing` | Load-Balancers, Target-groups, alb-listeners, alb-listener-rule, nlb-listener | network, security groups, certificates, buckets |
| `waf` | `waf` | Waf-ipsets, WAF-rulegroup, WAF | load balancers |
| `ecr_repositories` | `ecr` | ECR | |
| `eks` | `eks` | Deploy-eks (cluster, nodes, access, namespaces, RBAC) | network, security groups, IAM roles |
| `eks_addons` | nested in `eks` | Deploy-eks (add-ons, Helm controllers) | EKS clusters, certificates |
| `ecs_clusters` | `ecs` | Deploy-ecs | network, security groups, IAM, target groups, ECR, secrets |
| `rds_instances` | `rds` | RDS | network, security groups, secrets |
| `opensearch_domains` | `opensearch` | AWS-OpenSearch | network, security groups |
| `firehose_streams` | `firehose` | AWS-Firehose | buckets, OpenSearch, IAM roles |
| `lambdas`, `lambda_invocations` | `lambda` | Lambdas/Template, Lambda-invocations | network, IAM roles, buckets |
| `events` | `events` | Event-Bridge | Lambda functions |
| `cognito` | `cognito` | AWS-Cognito | certificates, Lambda functions |

IAM policies and users (`iam.policies`, `iam.users`) extend the existing `iam`
stack. Stacks receive each other's resources as objects, so CDK creates the
exports/imports and deploys producers first.

**Dev mirrors Terraform md-preproduction Dev**: the `ec2`, `default` and
`source-replication` roles/policies and `ec2` instance profile; the app, audit
and software buckets; the `key-pair` key pair; and the `*.dev.kahbrigthllc.com`
certificate. It also adds an EKS 1.36 cluster (`main`, one `t3.medium` node in
public subnets, scaling to two) and its add-ons. Names keep this repo's `dev`
segment, so nothing collides with Terraform's `development` resources in the
same account.

**EKS add-ons.** Native `AWS::EKS::Addon` resources, each configured separately:
`vpc-cni`, `coredns`, `kube-proxy`, `eks-pod-identity-agent`, `aws-ebs-csi-driver`,
`aws-efs-csi-driver`, `aws-fsx-csi-driver`, `aws-secrets-store-csi-driver-provider`,
`metrics-server`, `external-dns`, `cert-manager` and
`amazon-cloudwatch-observability`. Add-ons that call AWS get least-privilege EKS
Pod Identity roles.

Helm-based add-ons have typed settings in `eks_addons`:

| Key | Chart | AWS resources created |
| --- | --- | --- |
| `aws_load_balancer_controller` | aws-load-balancer-controller 3.6.0 | Pod Identity role with [AWS's policy](policies/aws-load-balancer-controller-v3.6.0.json) |
| `karpenter` | karpenter 1.15.0 (`oci://public.ecr.aws`) | controller role ([official policy](policies/karpenter-controller-v1.15.0.json)), interruption SQS queue, 5 EventBridge rules, `EC2NodeClass` + `NodePool` |
| `argocd` | argo-cd 10.10.5 | ALB ingress; optional Identity Center SAML via Dex |
| `nginx_ingress` | F5 NGINX Ingress Controller 2.8.0 | NLB with TLS at the load balancer |
| `gateway_api` | NGINX Gateway Fabric 2.8.0 | Gateway API v1.6.3 CRDs from [charts/gateway-api-crds](charts/gateway-api-crds) |
| `kubecost` | cost-analyzer 2.9.7 | optional ALB ingress |
| `awx_operator` | awx-operator 3.2.1 | optional AWX instance with ALB ingress |
| `storage_class` | — | default encrypted gp3 `StorageClass` |
| `helm_charts` | any | none (generic escape hatch) |

`load_balancer_defaults` sets the scheme, subnets, certificate, security groups
and ALB group shared by those ingresses/NLBs; dev puts ArgoCD and AWX on one
ALB. The community `ingress-nginx` project was retired in March 2026, so the
maintained F5 NGINX Ingress Controller replaces it.

Install order matters on a small node group: the core add-ons go first, then
Karpenter and its NodePool, then every other add-on (Karpenter adds nodes for
them). Charts that create Ingresses or LoadBalancer Services wait for the Load
Balancer Controller's webhook.

**Load balancers in dev** are created by Kubernetes (Ingress/Service objects
through the Load Balancer Controller), not by the `load_balancing` stack. That
stack remains available for load balancers outside the cluster.

**Deliberate differences from Terraform:**

- Synth needs no AWS credentials. Hosted zone IDs and SSO role ARNs
  (`[[admin_role]]`, `[[network_role]]`) are resolved at deploy time by a small
  read-only custom resource ([lookups.py](src/cognitech_cdk/constructs/lookups.py)).
- Secrets, private keys and passwords never appear in templates or outputs. Key
  pairs are generated by EC2 and copied into Secrets Manager inside AWS; RDS and
  OpenSearch master passwords are generated by Secrets Manager.
- Public resource policies are rejected. Terraform's
  `secrets_manager_policy.json` (`Principal: "*"`) is not used; the key-pair
  secret is readable only through IAM in this account.
- Buckets block all public access and enforce TLS; stateful resources are
  retained on stack deletion unless the YAML chooses `destroy`.
- EKS clusters are an exception: `removal_policy` defaults to `destroy`, so
  deleting their stack also deletes the cluster. Explicit `retain` remains
  available when needed. Deploy a policy change before destroying an existing
  stack; local changes cannot alter a deletion already in progress.
- WAF managed rule groups use `override_action` and `managed_rule_group_name`,
  and rule files may use the `{"rules": [...]}` shape.
- Firehose creates a scoped delivery role when no role is given (Terraform
  created an unused one).

**Before deploying dev, note:**

- The `ec2` role mirrors Terraform and attaches `AdministratorAccess`.
- The certificate's DNS validation records are shared with any Terraform
  certificate for the same domain; deleting the CDK stack can remove them.
- EKS grants cluster-admin only to the deploying role. Add `access_entries` for
  people who need `kubectl`.
- ArgoCD SSO is off until you create an Identity Center SAML application for
  `argocd.dev.kahbrigthllc.com` and fill in `sso_url` and `ca_pem_file`. Until
  then, use the `argocd-initial-admin-secret` in the cluster.
- Kubecost has no login, so dev does not expose it; use
  `kubectl -n kubecost port-forward svc/kubecost-cost-analyzer 9090`.
- Public ALBs/NLBs, Karpenter nodes, EKS, CloudWatch observability and Pod
  Identity have hourly costs. Nothing is created until you deploy a stack.

## Naming convention

`Common.name(*parts)` builds `{account_name}-{region_prefix}-{parts...}`, matching
the Terraform modules:

| Resource         | Pattern                                                   | Example                                            |
| ---------------- | --------------------------------------------------------- | -------------------------------------------------- |
| VPC              | `{account}-{region}-{vpc}-vpc`                          | `int-production-use1-dev-vpc`                    |
| Internet gateway | `{account}-{region}-{vpc}-igw`                          | `int-production-use1-dev-igw`                    |
| Subnet           | `{account}-{region}-{vpc}-{subnet}-{type}-{ordinal}`    | `int-production-use1-dev-edge-public-primary`    |
| Route table      | `{account}-{region}-{vpc}-{subnet}-{ordinal}-{type}-rt` | `int-production-use1-dev-edge-primary-public-rt` |
| NAT gateway      | `{account}-{region}-{vpc}-{subnet}-ngw-{ordinal}`       | `int-production-use1-dev-edge-ngw-primary`       |
| Elastic IP       | `{account}-{region}-{vpc}-{subnet}-eip-{ordinal}`       | `int-production-use1-dev-edge-eip-primary`       |
| Security group   | `{account}-{region}-{name}-sg`                          | `int-production-use1-eks-nodes-sg`               |
| IAM role         | `{account}-{region}-{environment}-{name}-role`         | `int-production-use1-dev-eks-nodes-role`         |
| Instance profile | `{account}-{region}-{environment}-{name}-profile`      | `int-production-use1-dev-eks-nodes-profile`      |

When a subnet group keeps its default name, the repeated word is dropped:
`public_subnet_name: public` gives `int-production-use1-dev-public-primary`, not
`...-public-public-primary`.

Tags are applied once in the stack with `Tags.of(self)`, which CDK propagates to
every taggable resource. `Name` is set per resource and overrides the inherited
value.

---

## Deploying manually

### Selecting the environment

The environment is a folder name under `deployments/` — `dev` or `prod` today.
It is resolved in this order, first match wins:

| Priority | Source                  | Example                         |
| -------- | ----------------------- | ------------------------------- |
| 1        | `--context env=<name>`  | `cdk deploy --context env=prod` |
| 2        | the `ENV` variable      | `export ENV=prod`               |
| 3        | `DEFAULT_ENV` in `app.py` | `dev`                         |

**Setting `ENV` is the recommended way.** Set it once and every later `cdk`
command in that terminal uses it, so there is no flag to forget and no risk of
running `diff` against one environment and `deploy` against another.

```bash
# WSL / Linux / macOS — set this before any cdk command
export ENV=dev
```

```powershell
# Windows PowerShell
$env:ENV = "dev"
```

Confirm it took effect before deploying — `cdk ls` prints the stack name for the
selected environment and needs no AWS credentials:

```bash
$ echo $ENV
dev
$ cdk ls
md-preproduction-use1-dev-network
```

Then run the rest with no environment flag at all:

```bash
cdk synth
cdk diff   --profile admin-dev
cdk deploy --profile admin-dev
```

Managing the variable:

| Task                       | Bash / WSL              | PowerShell                       |
| -------------------------- | ----------------------- | -------------------------------- |
| Set it                     | `export ENV=prod`       | `$env:ENV = "prod"`              |
| Check what is set          | `echo $ENV`             | `$env:ENV`                       |
| Clear it                   | `unset ENV`             | `Remove-Item Env:\ENV`           |
| Use it for one command     | `ENV=prod cdk deploy`   | `$env:ENV="prod"; cdk deploy`    |

It lasts until you clear it or close the terminal — a new terminal starts
without it and falls back to `dev`.

**Overriding for a one-off.** `--context` beats `ENV`, so you can check another
environment without disturbing your session:

```bash
export ENV=prod
cdk diff --context env=dev     # inspects dev; ENV is still prod afterwards
```

**A wrong name fails immediately**, before anything reaches AWS:

```
Unknown environment 'staging'. Known: dev, prod.
Set it with --context env=<name> or ENV=<name>.
```

> `ENV` is a generic variable name. If `cdk` ever targets an environment you did
> not expect, run `echo $ENV` first — something else in your shell may be setting it.

### A full deploy, start to finish

**dev:**

```bash
source .venv-linux/bin/activate   # 1. activate the venv (every new terminal)
export ENV=dev                    # 2. choose the environment
aws sso login --profile admin-dev # 3. sign in to the matching account

pytest -q                         # 4. does it still build?
cdk ls                            #    -> int-production-use1-dev-network
cdk diff   --profile admin-dev    #    what changes in AWS?
cdk deploy --profile admin-dev    # 5. apply it
```

**prod** is the same with two words changed:

```bash
export ENV=prod
aws sso login --profile admin-prod

cdk diff   --profile admin-prod   #    -> int-production-use1-prd-network
cdk deploy --profile admin-prod
```

Steps 1–3 are once per terminal; steps 4–5 are the loop you repeat as you work.

### Prerequisites

**An activated venv.** `cdk.json` runs `python app.py`, so you **must** be inside
one — that is what puts a plain `python` on `PATH`. Without it you get
`/bin/sh: 1: python: not found` (exit code 127), because Ubuntu only ships
`python3`. A venv built on Windows cannot be used from WSL and vice versa; keep
one per platform, both gitignored.

**AWS credentials for the right account.** Your profile must resolve to the same
`account_id` as the environment you selected, or CDK stops with:

```
Need to perform AWS calls for account X, but the current credentials are for Y
```

That check is deliberate — it stops dev config reaching the prod account.

```bash
aws sso login --profile admin-dev
aws sts get-caller-identity --profile admin-dev
```

**A bootstrapped account**, once per account *and* region:

```bash
cdk bootstrap aws://<account-id>/<region> --profile <your-profile>
```

This creates the `CDKToolkit` stack: an S3 asset bucket, an ECR repo and five IAM
roles. The important one is `cdk-hnb659fds-cfn-exec-role-...`, which
CloudFormation uses to create your resources — `AdministratorAccess` by default.
For a HIPAA account, narrow it:

```bash
cdk bootstrap aws://<account-id>/<region> \
  --cloudformation-execution-policies arn:aws:iam::aws:policy/PowerUserAccess,arn:aws:iam::aws:policy/IAMFullAccess
```

### The cdk commands

| Command      | What it does                                                                                 | Needs AWS? |
| ------------ | -------------------------------------------------------------------------------------------- | ---------- |
| `pytest -q`  | Runs the unit tests against the synthesized template.                                        | no         |
| `cdk ls`     | Lists the stack names for that environment. Confirms the config parses.                      | no         |
| `cdk synth`  | Writes the CloudFormation template to `cdk.out/`. Prints it unless you pass `--quiet`.       | no         |
| `cdk diff`   | Compares the synthesized template against what is **actually deployed**, and prints changes. | yes        |
| `cdk deploy` | Creates a change set and executes it, streaming the result.                                  | yes        |

The first three run offline because the availability zones come from the config
rather than an AWS lookup — which is also why CI can run them before assuming a
role.

`cdk diff` is the one that matters. It is read-only and safe to run as often as
you like; run it before every deploy. A `+` means a resource is being created,
`-` destroyed, and `[~]` modified — watch for `(requires replacement)`, which
means the resource is deleted and recreated.

`cdk deploy` prompts before making IAM or security-group changes. Add
`--require-approval never` to skip that (CI does), and `--progress events` to
stream CloudFormation events as they happen.

Tear down with `cdk destroy`, which honours `ENV` the same way.

### Useful flags

| Flag                         | Purpose                                          |
| ---------------------------- | ------------------------------------------------ |
| `--require-approval never` | skip the IAM/security prompt (used in CI)        |
| `--progress events`        | stream CloudFormation events                     |
| `--outputs-file out.json`  | write the VPC ID and subnet IDs to a file        |
| `--hotswap`                | non-production fast path; never use against prod |

---

## Deploying with GitHub Actions

No static AWS keys are stored anywhere: each run exchanges a short-lived GitHub
OIDC token for an IAM role.

### Prerequisites

Work through these once per AWS account before the first pipeline run. Steps 1
and 2 are the same ones you already did for local deploys.

| # | Prerequisite | How to check |
| - | ------------ | ------------ |
| 1 | An `env.yaml` with the right `account_id` | `ENV=dev cdk ls` |
| 2 | The account is bootstrapped | `aws cloudformation describe-stacks --stack-name CDKToolkit --profile <p>` |
| 3 | GitHub OIDC provider exists | `aws iam list-open-id-connect-providers --profile <p>` |
| 4 | Deploy role exists | `aws iam get-role --role-name github-oidc-cdk-deploy-dev --profile <p>` |
| 5 | A GitHub Environment named exactly like the folder | Settings → Environments |
| 6 | The role ARN written into the workflow | `grep role-to-assume .github/workflows/*.yml` |

**No GitHub secrets or variables are required.** The role ARN and region are
written directly into each workflow — see Step 4.

> **Always pass `--profile`.** Your default AWS credentials may point at a
> different account, in which case these checks report "does not exist" for
> things that are present. Confirm which account you are querying first:
>
> ```bash
> aws sts get-caller-identity --profile <your-profile> --query Account --output text
> ```
>
> It must match `account_id` in `deployments/<env>/env.yaml`.

You also need admin rights on the AWS account (to create IAM roles) and admin on
the GitHub repo (to create Environments).

#### Step 1 — Bootstrap the account

Skip if you already deployed locally; it is the same `CDKToolkit` stack.

```bash
cdk bootstrap aws://533267408704/us-east-1 --profile admin-mdpp
```

Verify:

```bash
aws cloudformation describe-stacks --stack-name CDKToolkit \
  --profile admin-mdpp --query 'Stacks[0].StackStatus' --output text
# CREATE_COMPLETE
```

#### Step 2 — Create the OIDC provider and roles

`cdk bootstrap` does **not** create these. The helper script does, and it is
idempotent — re-running updates policies in place and deletes nothing.

```bash
./scripts/create_github_oidc_roles.sh --yes \
  --account-id 533267408704 \
  --environment dev \
  --repo KahBrightTech/cognitech-AWS-CDK-repo \
  --region us-east-1 \
  --profile admin-mdpp
```

Run it with no arguments to be prompted for each value, pre-filled from your AWS
profile, the git remote and the `deployments/` folder.

It refuses to run if your credentials are for a different account, or if the
account is not bootstrapped yet. Repeat it per account — for prod, pass
`--environment prod` with that account's ID and profile.

It creates:

| Resource | Purpose |
| -------- | ------- |
| OIDC provider for `token.actions.githubusercontent.com` | lets GitHub tokens be exchanged for AWS credentials |
| `github-oidc-cdk-deploy-<env>` | assumed by the deploy job; may only `sts:AssumeRole` the `cdk-hnb659fds-*` roles |
| `github-oidc-cdk-preview-<env>` | trusted from `main` without a GitHub environment; AWS `ReadOnlyAccess` plus permission to assume only the bootstrap lookup role |

### Preview and final deployment approval

The deployment workflow has two jobs:

1. `preview` runs tests, synth and `cdk diff --no-change-set` without an
   environment approval. It uses `AWS_PREVIEW_ROLE_ARN`, not the deployment role.
2. `apply` runs only for manual `deploy` or `destroy`, after preview succeeds.
   It references the GitHub environment selected by `ENV` and applies the
   synthesized assembly uploaded by preview, rather than synthesizing again.

Configure **Settings → Environments → dev → Required reviewers** to gate the
second job. Without required reviewers, merely referencing the environment does
not require approval. Optionally prevent self-review and administrator bypass,
and restrict deployment branches to `main`. Native required reviewers for
private repositories require GitHub Enterprise; see
[GitHub's protection-rule availability](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments#required-reviewers).

Set `ENV`, `AWS_ROLE_ARN`, `AWS_PREVIEW_ROLE_ARN` and `AWS_REGION` at the top of
the deployment workflow. The YAML alias connects `ENV` to the apply job's
GitHub environment. Re-run the role-setup script above to provision the preview
role before using this split; the existing deployment trust remains
environment-bound. Preview trust permits only `refs/heads/main`, so dispatches
from other branches cannot authenticate for preview. Its AWS-managed
`ReadOnlyAccess` is broad account read access, not a resource-scoped custom
policy; it cannot assume deployment, publishing or execution roles.

The preview artifact is retained for seven days. Approve within that period;
if it expires, start a new run to generate and review a fresh diff.

### Automatic stack-dropdown updates

The sync workflow edits files under `.github/workflows`. The built-in
`GITHUB_TOKEN` cannot push those edits, even with `contents: write`.

Create a fine-grained PAT with access to **only this repository**, granting
**Contents: read and write** and **Workflows: read and write**. Store it as
repository Actions secret **`WORKFLOW_SYNC_TOKEN`** under
**Settings → Secrets and variables → Actions**. The token's owner must have
repository write access; organizational approval policies and branch rules
still apply. Set an expiry and rotate the secret before it expires. Do not put
the token in workflow YAML.

PAT-authenticated pushes trigger other matching workflows. Sync only edits
deploy workflow files, which are outside its own push-path filters, preventing
a sync loop. If direct pushes to `main` are blocked, run
`python scripts/sync_stack_choices.py` locally and submit the generated changes
through your normal pull-request process.

Verify:

```bash
aws iam get-role --role-name github-oidc-cdk-deploy-dev \
  --profile admin-mdpp --query 'Role.Arn' --output text
# arn:aws:iam::533267408704:role/github-oidc-cdk-deploy-dev
```

Copy that ARN — it goes straight into the workflow in Step 4.

> **Pull requests need no AWS access.** `pr.yml` only runs tests and `cdk synth`,
> which work offline, and it does not request `id-token: write`. So the script
> creates no PR-time role. If you later add a credentialed `cdk diff` to the PR
> workflow, create a read-only role for it then — with the trust policy pinned
> to the `pull_request` subject.

#### Step 3 — Create the GitHub Environment

Settings → Environments → **New environment**, named **exactly** like the folder
under `deployments/`: `dev`.

The name must match. The workflow sets `environment: dev`, and the OIDC trust
policy expects the claim
`repo:KahBrightTech/cognitech-AWS-CDK-repo:environment:dev`. A mismatch fails
with `Not authorized to perform sts:AssumeRoleWithWebIdentity`.

GitHub is migrating to **immutable subject claims**, which splice the numeric
owner and repository IDs into that string:

```
repo:KahBrightTech@202037050/cognitech-AWS-CDK-repo@1383489754:environment:dev
```

Names can be recycled, IDs cannot, so the ID form is the one AWS should pin to.
`create_github_oidc_roles.sh` looks both IDs up from the GitHub API and trusts
the two forms, so the roles keep working before and after the switch. Pass
`--owner-id` / `--repo-id` if the lookup cannot reach the API.

This is also where **Required reviewers** live — add them on any environment
that should pause for approval before deploying.

The deployment environment needs no AWS secrets because authentication uses OIDC.
Configure its required reviewers to enforce approval on apply; separately add
the repository secret `WORKFLOW_SYNC_TOKEN` for automatic dropdown updates.

#### Step 4 — Put the IAM role ARN in the workflow

**This is where the IAM role goes** — written directly into the workflow, not
stored as a secret:

```yaml
- name: Configure AWS credentials
  uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::533267408704:role/github-oidc-cdk-deploy-dev
    aws-region: us-east-1
    role-session-name: cdk-deploy-dev-${{ github.run_id }}
```

Get the exact ARN with:

```bash
aws iam get-role --role-name github-oidc-cdk-deploy-dev \
  --profile admin-mdpp --query 'Role.Arn' --output text
```

**Why this is safe.** A role ARN is not a credential — it is an identifier. What
actually stops anyone else assuming the role is its **trust policy**, which only
accepts an OIDC token from this repository carrying the claim
`environment:dev`. Publishing the ARN does not weaken that.

The account ID inside the ARN is likewise already in
`deployments/dev/env.yaml`, so putting it in the workflow exposes nothing new.

`aws-region` is hardcoded for the same reason. Keep it in step with `region:` in
that environment's `env.yaml` — they are two places that must agree.

> If you would rather keep the ARN out of the repo, use a GitHub Environment
> secret instead:
> `role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}`, with the secret defined
> on each Environment. GitHub scopes secrets per environment, so the same name
> resolves to the right ARN in each workflow.

### Running a deploy

There is **one workflow per environment**, so there is nothing to pick and no
way to run against the wrong account by mistake:

| Workflow file | Shows in Actions as | Deploys | Into |
| ------------- | ------------------- | ------- | ---- |
| `deploy-primary-mdpp-dev.yml` | Deploy primary mdpp dev | `deployments/dev/` | md-preproduction `533267408704`, us-east-1 |

The naming pattern is `deploy-<region role>-<account>-<environment>`, so a second
region would be `deploy-secondary-mdpp-dev.yml` and another account
`deploy-primary-intprod-prod.yml`.

#### How it runs: review first, then act

Nothing is ever created or deleted automatically. A push shows you the diff; you
then choose what to do with it.

**1. Push to `main` → `cdk diff` only.**

```yaml
on:
  push:
    branches: [main]
    paths:
      - "deployments/dev/**"      # this environment's config
      - "src/**"                  # shared constructs and stacks
      - "app.py"
      - "cdk.json"
      - "requirements*.txt"
      - ".github/workflows/deploy-primary-mdpp-dev.yml"
```

The run stops after the diff, which is written to the **run summary** — open the
run and read it at the top, no log digging. The deploy and destroy steps are
guarded by `if: inputs.action == ...`, and a push supplies no inputs, so they
cannot fire.

The `paths` filter keeps environments independent. Editing
`deployments/prod/env.yaml` does **not** match this list, so it will not trigger
a dev run. Editing `src/` matches *every* environment's workflow, which is
correct — shared construct code changes them all.

**2. Reviewed it? → Actions → the workflow → Run workflow**, and pick:

| `action` | What happens |
| -------- | ------------ |
| `diff` (default) | Re-runs the diff. Changes nothing. |
| `deploy` | Diff, then `cdk deploy` — creates or updates the stack. |
| `destroy` | Diff, then `cdk destroy` — **deletes the stack and everything in it.** |

`cdk diff` runs first in all three cases, so the log always records what the run
was about to do.

> **Destroy needs explicit confirmation.** Type `dev` into the confirm box or the
> job fails before it reaches AWS:
>
> ```
> Error: Destroy requires the confirm box to contain exactly: dev
> ```
>
> There is no retention policy on the VPC, so destroy really does delete it.

To gate this further, add **Required reviewers** to the `dev` GitHub Environment
— the run then pauses before *any* step until someone approves. That is how you
would protect a production environment.

Add the same `on:` block to each new environment's workflow, changing only the
`deployments/<name>/**` line and the workflow's own filename.

Each workflow hardcodes everything about its target:

```yaml
environment: dev      # GitHub Environment: required reviewers live here
env:
  ENV: dev            # app.py reads this, so no --context flag anywhere
...
    role-to-assume: arn:aws:iam::533267408704:role/github-oidc-cdk-deploy-dev
    aws-region: us-east-1
```

> **The `environment:` value is not free-form.** It must match the folder under
> `deployments/` *and* the GitHub Environment name, because the OIDC trust policy
> expects the claim `repo:<org>/<repo>:environment:dev` (or its immutable
> `repo:<org>@<id>/<repo>@<id>:environment:dev` form). The *file name* and the
> `name:` field can be anything you like.

What runs, in order:

| Step | Needs AWS? | When |
| ---- | ---------- | ---- |
| Confirm check (destroy only) | no | `action: destroy` |
| Install Python, Node and the CDK CLI | no | always |
| `pytest -q` | no | always |
| `cdk synth --quiet` | no | always |
| Assume the deploy role via OIDC | — | always |
| `cdk diff` | yes | always |
| `cdk deploy` | yes | `action: deploy` |
| `cdk destroy --force` | yes | `action: destroy` |

### On every pull request

`pr.yml` runs automatically: `pytest` plus `cdk synth` for every environment in
parallel. It needs no AWS credentials and no secrets, because the availability
zones come from the config rather than an AWS lookup.

### Adding an environment

1. Copy `deployments/dev/` to `deployments/<name>/` and edit `account_id`,
   `region`, `common` and `network`.
2. Bootstrap that account and region (Step 1).
3. Run the OIDC script with `--environment <name>` (Step 2).
4. Create the GitHub Environment with that name (Step 3).
5. Copy `.github/workflows/deploy-primary-mdpp-dev.yml` to a new file, then
   change `name:`, the `concurrency.group`, `environment:`, `ENV:`,
   `role-to-assume:`, `aws-region:`, and the two environment-specific entries
   under `on.push.paths` (`deployments/<name>/**` and the workflow's own
   filename).
6. Add `<name>` to the `environment:` matrix in `.github/workflows/pr.yml`.

### Which role does what

The deploy role never creates a VPC. It only creates and executes a change set;
CloudFormation does the work using the execution role from `cdk bootstrap`.

```
GitHub job
  └─ OIDC token → github-oidc-cdk-deploy-<env>   (no resource permissions)
       └─ sts:AssumeRole → cdk-hnb659fds-deploy-role   (CreateChangeSet)
            └─ iam:PassRole → cdk-hnb659fds-cfn-exec-role
                 └─ CloudFormation assumes it and calls ec2:CreateVpc, ...
```

So a compromised runner cannot call `ec2:*` or `iam:*` directly — it has to go
through a template and visible stack events. The exec role's policy is the real
blast radius, which is why narrowing `--cloudformation-execution-policies` at
bootstrap time matters for a HIPAA account.

| Symptom | Role at fault |
| ------- | ------------- |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | GitHub Environment name does not match the folder under `deployments/` |
| `not authorized to perform: iam:PassRole on .../cfn-exec-role` | deploy role, or a bootstrap/qualifier mismatch |
| Fails before any stack events appear | deploy or file-publishing role |
| Stack rolls back with `AccessDenied` on `ec2:CreateVpc` | cfn-exec role — re-bootstrap with broader policies |

---

## Adding constructs and stacks

1. **Construct** — new file in `src/cognitech_cdk/constructs/`, subclassing
   `Construct`, taking `common: Common` plus its own frozen props dataclass as
   keyword-only arguments. Name resources with `common.name(...)` (or
   `common.abbreviated_name(...)` where a 32-character cap applies). Do not
   re-apply the common tags — the stack already tags the whole tree.
2. **Config** — add a `from_dict` to the props dataclass and a field on
   `Environment` in `src/cognitech_cdk/common/environment.py`, then add the key
   to every `deployments/<env>/env.yaml`.
3. **Stack** — add the construct to an existing stack, or add a module under
   `src/cognitech_cdk/stacks/` and register it in `app.py`.
4. **Test** — add assertions in `tests/unit/`.

Nothing has to be added to the deploy workflows: they ask `cdk list` what the
environment builds, and the `sync-stack-choices` workflow rewrites their stack
dropdown from the same source once the change is on `main`. `pytest` fails if a
committed dropdown no longer matches the app, so the fix is to run
`python scripts/sync_stack_choices.py` and commit the result.

Suggested stack grouping, roughly one per lifecycle:

| Stack                 | Holds                                                    |
| --------------------- | -------------------------------------------------------- |
| `network_stack.py`  | VPC, subnets, VPC endpoints, transit gateway attachments |
| `security_stack.py` | security groups, WAF                                     |
| `iam_stack.py`      | IAM service roles and EC2 instance profiles              |
| `platform_stack.py` | SSM parameters, secrets, ECR                             |
| `edge_stack.py`     | ALB/NLB, target groups, ACM certificates, Route 53       |
| `data_stack.py`     | RDS, DynamoDB, EFS, OpenSearch                           |
| `compute_stack.py`  | EC2, launch templates, ASGs, ECS, EKS, Lambda            |

The VPC is a normal `ec2.IVpc`, so anything downstream can use it directly:

```python
stack.network.vpc                          # ec2.IVpc
stack.network.public_subnets               # list[ec2.ISubnet]
stack.network.private_subnets              # list[ec2.ISubnet]
stack.network.subnet_selection("private")  # for vpc_subnets=
```

Between stacks, pass the construct object into the second stack's constructor —
CDK creates the export/import and orders the deployments. Avoid circular
references; if two stacks need each other, the shared piece belongs in a third
stack or in SSM Parameter Store.

---

## Troubleshooting

| Symptom                                                                            | Cause and fix                                                            |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `/bin/sh: 1: python: not found` / `Subprocess exited with error 127`           | No activated venv, or a Windows venv used from WSL.                      |
| `ModuleNotFoundError: No module named 'yaml'`                                    | `pip install -r requirements-dev.txt`                                  |
| Pylance says`Import "aws_cdk" could not be resolved`                             | VS Code has an interpreter with no packages, or the wrong workspace root. See **Editor setup** below. |
| Pylance says`Import "cognitech_cdk..." could not be resolved`                    | `src/` is not on the analysis path. See **Editor setup** below.          |
| `azs must be between 1 and 4`                                                    | `azs` in `env.yaml` is outside 1–4.                                 |
| `lists N CIDRs but azs is M`                                                     | Give one CIDR per AZ, in zone order.                                     |
| `subnet CIDR ... is outside the VPC range`                                       | The range is not inside `cidr_block`.                                  |
| `subnet CIDRs ... overlap`                                                       | Two explicit ranges collide.                                             |
| `no free /N block left`                                                          | `cidr_mask` is too large for the VPC; widen `cidr_block` or lower the mask. |
| `private subnets need at least one NAT gateway`                                  | Set`private_subnets: false`, or raise `nat_gateways`.                |
| `points at '...', which is not a group in this environment`                      | A rule's`source`/`destination` is a typo. Use a group name from the same list, a CIDR, an `sg-` id or a `pl-` id. |
| `'...' is defined twice` / `duplicate ingress rule`                            | Two groups share a name, or a group repeats the same peer and ports.     |
| `allow_all_outbound is true`                                                     | A group lists`egress` rules and leaves outbound open; drop one.        |
| `ports must be a number` / `outside 0-65535` / `runs backwards`              | Use`443` or `30000-32767`, low end first.                            |
| `common.tags is missing ...`                                                     | Add the required tag keys.                                               |
| `Need to perform AWS calls for account X, but the current credentials are for Y` | `account_id` does not match your profile.                              |
| `ExpiredToken` / `InvalidClientTokenId`                                        | `aws sso login --profile <your-profile>`                               |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity`                        | The `sub` in the token does not match the role's trust policy. Re-run `scripts/create_github_oidc_roles.sh` — it trusts both the name-based and the immutable ID-based claim. Otherwise check the GitHub Environment name matches the folder under `deployments/`. |
| `This CDK deployment requires bootstrap stack version X`                         | Re-run`cdk bootstrap`.                                                 |

### Editor setup

The repo lives on the WSL filesystem, so there are two virtual environments:

| Venv          | Used by                                  |
| ------------- | ---------------------------------------- |
| `.venv-linux` | running `pytest` and `cdk` from WSL/bash |
| `.venv-win`   | Pylance when VS Code runs on Windows     |

Both are gitignored. Recreate either with:

```bash
# WSL / Linux
python3 -m venv .venv-linux && source .venv-linux/bin/activate
pip install -r requirements-dev.txt
```

```powershell
# Windows
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements-dev.txt
```

**VS Code reads `.vscode/settings.json` from the workspace root.** If you open a
*parent* folder instead of this repo, the repo's own settings are ignored and every
import looks unresolved — including `aws_cdk`. Two ways to fix it:

- Open this repo directly: **File → Open Folder** → `cognitech-AWS-CDK-repo`, or
- keep a `.vscode/settings.json` in the parent folder with each path prefixed by
  `cognitech-AWS-CDK-repo/`.

Then run **Python: Select Interpreter** and pick the one matching how the folder
was opened — `.venv-win\Scripts\python.exe` on Windows, or `.venv-linux/bin/python`
after **WSL: Reopen Folder in WSL**.

The settings that matter either way:

```jsonc
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv-win/Scripts/python.exe",
  // without this, cognitech_cdk.* imports never resolve
  "python.analysis.extraPaths": ["${workspaceFolder}/src"]
}
```
