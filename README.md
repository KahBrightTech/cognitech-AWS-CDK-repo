# cognitech-AWS-CDK-repo

AWS infrastructure built with the AWS CDK (Python).

See [DEPLOYMENT.md](DEPLOYMENT.md) for the deploy walkthrough and the pipeline setup.

## Layout

```
app.py                              entrypoint: picks an environment, builds the stacks
cdk.json
pyproject.toml                      pytest config (adds src/ to the path)

src/cognitech_cdk/
  common/config.py                  config schema + environment discovery
  common/naming.py                  primary/secondary/... helper
  constructs/network.py             VPC, subnets, route tables, IGW, NAT gateways
  stacks/network_stack.py           deployment unit

deployments/                        one folder per environment
  uat/env.yaml
  prod/env.yaml

scripts/select_environments.py      feeds the GitHub Actions matrix
tests/unit/
.github/workflows/
```

**Constructs** are reusable building blocks; **stacks** group them into a deployable
CloudFormation stack. A construct is not deployable on its own.

## Defining a network

Each `subnet_group` is one tier, replicated across every availability zone.

```yaml
networks:
  - name: uat
    cidr_block: 10.20.0.0/16
    availability_zones: [us-east-1a, us-east-1b]
    nat_gateways: 1
    subnet_groups:
      - name: edge
        type: public
        cidrs: [10.20.0.0/20, 10.20.16.0/20]   # one per AZ, same order
      - name: app
        type: private
        cidrs: [10.20.64.0/20, 10.20.80.0/20]
```

That produces the VPC, the subnets, a route table per subnet, the internet gateway, the
NAT gateways and all the default routes.

### Availability zones

| Key | Behaviour |
| --- | --------- |
| `availability_zones: [us-east-1a, us-east-1b]` | exactly these zones, in this order |
| `az_count: 3` | derives `us-east-1a`, `us-east-1b`, `us-east-1c` from the region |

Set one or the other. Both are resolved at synth time with no AWS API call, so `cdk synth`
works without credentials.

### Subnet CIDRs

| Key | Behaviour |
| --- | --------- |
| `cidrs: [...]` | exactly these ranges, one per AZ |
| `cidr_mask: 20` | carves the lowest free `/20` blocks out of the VPC range |

Set one or the other per group; you can mix styles between groups in the same VPC, and
explicit ranges are reserved before anything is carved. The loader rejects CIDRs that fall
outside the VPC range, overlap each other, or do not match the AZ count.

> Carved CIDRs depend on declaration order. Inserting a group ahead of an existing one
> shifts the ranges below it and CloudFormation will **replace** those subnets. Append new
> groups at the end, or use explicit `cidrs`.

### Subnet types and NAT

| `type` | Egress | Requires |
| ------ | ------ | -------- |
| `public` | direct, via the internet gateway | — |
| `private` | outbound only, via a NAT gateway | `nat_gateways >= 1` and a `public` group |
| `isolated` | none | — |

`nat_gateways` defaults to `0`, so a VPC with no `private` groups creates no NAT gateways
and no Elastic IPs. The loader rejects the combinations that silently cost money or break:
a `private` group with no NAT, NAT gateways with no `public` group to host them, and NAT
gateways that nothing would route through.

## Naming and tagging

`CommonProps.resource_name(*parts)` builds
`{account_name}-{region_prefix}-{qualifier}-{parts...}`:

| Resource | Name |
| -------- | ---- |
| VPC | `int-production-use1-cdk-uat-vpc` |
| Internet gateway | `int-production-use1-cdk-uat-igw` |
| Subnet | `int-production-use1-cdk-uat-app-private-primary` |
| Route table | `int-production-use1-cdk-uat-app-private-primary-rtb` |
| NAT gateway | `int-production-use1-cdk-uat-primary-natgw` |
| Elastic IP | `int-production-use1-cdk-uat-primary-nat-eip` |

`qualifier: cdk` marks these resources as CDK-built so they never collide with the
Terraform estate on resources that require unique names. Delete the key to drop the segment.

Common tags live under `common.tags` in each environment file and are applied once, in the
stack constructor, with `Tags.of(self)` — CDK propagates them to every taggable resource.
`Name` is deliberately not a common tag; it is set per resource. The loader rejects a
config that puts `Name` in `common.tags` or omits any of `Build-method`, `Environment`,
`ManagedBy`, `Owner`, `Compliance`.

## Local usage

`cdk.json` runs the app with `python app.py`, so **always work inside an activated virtual
environment**. A venv built on Windows cannot be used from WSL and vice versa — keep one
per platform.

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
npm install -g aws-cdk@2
```

```bash
# WSL / Linux
python3 -m venv .venv-linux
source .venv-linux/bin/activate
pip install -r requirements-dev.txt
npm install -g aws-cdk@2
```

Then, with the venv active:

```bash
pytest                                  # asserts against the synthesized template
cdk ls     --context env=uat
cdk synth  --context env=uat
cdk diff   --context env=uat            # shows what a deploy would change
cdk deploy --all --context env=uat
```

First deploy in an account/region needs `cdk bootstrap aws://<account-id>/<region>`.

If `cdk` reports `/bin/sh: 1: python: not found` (exit code 127), the venv is not
activated — Ubuntu has no `python`, only `python3`. See
[DEPLOYMENT.md](DEPLOYMENT.md#41-local-toolchain).

## Adding to the stack

1. Add a construct under `src/cognitech_cdk/constructs/` taking `common: CommonProps` plus
   its own typed props, naming resources with `common.resource_name(...)`. Don't re-apply
   common tags — the stack already did.
2. Add the matching dataclass in `src/cognitech_cdk/common/config.py` and the keys in each
   `deployments/<env>/env.yaml`.
3. Instantiate it from a stack, or add a new stack and register it in `app.py`.
4. Add assertions in `tests/unit/`.

Pass values between constructs as objects — CDK resolves them to `Ref`/`Fn::GetAtt`
automatically. Across stacks, pass the construct object and CDK creates the
export/import plus the deployment ordering.

## Adding an environment

Create `deployments/<name>/env.yaml`. It is discovered automatically, deployable with
`--context env=<name>`, and picked up by the pipelines with no workflow edits.
`deploy_order` controls promotion order.

## GitHub Actions

Authentication uses GitHub OIDC; no long-lived AWS keys are stored.

One-time AWS setup per account:

1. Create the OIDC provider for `token.actions.githubusercontent.com`.
2. Create two roles trusting it, scoped to this repo:
   - a **plan** role: `ReadOnlyAccess` + `sts:AssumeRole` on `cdk-hnb659fds-lookup-role-*`
   - a **deploy** role: `sts:AssumeRole` on the `cdk-hnb659fds-*-role-*` bootstrap roles

| Where | Name | Value |
| ----- | ---- | ----- |
| Repo secret | `AWS_PLAN_ROLE_ARN_UAT` | plan role ARN (uat) |
| Repo secret | `AWS_PLAN_ROLE_ARN_PROD` | plan role ARN (prod) |
| Environment `uat` / `prod` | `AWS_DEPLOY_ROLE_ARN` | deploy role ARN |
| Repo variable | `AWS_REGION` | e.g. `us-east-1` |

Add required reviewers to the `prod` GitHub Environment to gate production deploys.

- `.github/workflows/cdk-pr.yml` — on PR: runs the tests once, works out which environments
  the PR affects, then runs `cdk diff` for each and comments the result.
- `.github/workflows/cdk-deploy.yml` — on push to `main` (or manual dispatch): deploys only
  the environments whose `deployments/<env>/` folder changed; a change under `src/` or
  `app.py` deploys all of them, in `deploy_order`, one at a time.
