# cognitech-AWS-CDK-repo

AWS CDK (Python) infrastructure, structured to mirror the Terraform module repo.

> **Deploying for the first time?** See [DEPLOYMENT.md](DEPLOYMENT.md) for the full
> walkthrough: bootstrap, `cdk diff`/`deploy`, the GitHub Actions pipelines, and where each
> remaining construct will live.

## How this maps to Terraform

| Terraform                       | CDK                                           |
| ------------------------------- | --------------------------------------------- |
| `modules/vpc`, `modules/subnets`| `src/cognitech_cdk/constructs/*.py`           |
| Root module calling `module {}` | `src/cognitech_cdk/stacks/network_stack.py`   |
| `*.tfvars`                      | `deployments/<env>/env.yaml`                  |
| `variables.tf` types            | dataclasses in `src/cognitech_cdk/common/config.py` |
| `outputs.tf`                    | `CfnOutput` at the bottom of the stack        |
| Workspaces / separate backends  | `cdk deploy --context env=<name>`             |
| `terraform plan`                | `cdk diff`                                    |
| S3 state + DynamoDB lock        | CloudFormation (no state file to manage)      |

Yes — **constructs are the modules, stacks call the constructs**. A construct is not
deployable on its own; a stack is the deployment unit (one CloudFormation stack).

## Layout

```
app.py                              # entrypoint: picks env, instantiates stacks
cdk.json
pyproject.toml                      # pytest config (adds src/ to the path)

src/cognitech_cdk/                  # <- all reusable code lives here
  common/config.py                  # typed config schema + env discovery
  common/naming.py                  # primary/secondary/... naming helper
  constructs/vpc.py                 # VPC + internet gateway
  constructs/subnets.py             # public/private subnets + route tables
  constructs/nat_gateway.py         # EIP + NAT gateways
  stacks/network_stack.py           # wires the constructs together

deployments/                        # <- one folder per environment
  uat/env.yaml                      # account, region, tags, CIDRs, deploy_order
  prod/env.yaml

scripts/select_environments.py      # feeds the GitHub Actions matrix
tests/unit/
.github/workflows/                  # PR diff + deploy pipelines
```

Adding an environment is one folder: `deployments/<name>/env.yaml`. It is discovered
automatically by `available_environments()`, deployable with `--context env=<name>`, and
picked up by the pipelines with no workflow edits. `deploy_order` controls promotion order.

## Naming and tagging

`CommonProps.resource_name(*parts)` builds
`{account_name}-{region_prefix}-{parts...}`, matching the Terraform modules:

| Call | Result |
| ---- | ------ |
| `resource_name("uat", "vpc")` | `int-production-use1-uat-vpc` |
| `resource_name("uat", "igw")` | `int-production-use1-uat-igw` |
| `resource_name("uat", "app", "private", "primary")` | `int-production-use1-uat-app-private-primary` |

Common tags live in `common.tags` in each config file and are applied once in the stack
constructor via `Tags.of(self)` — CDK propagates them to every taggable resource, which is
the `merge(var.common.tags, ...)` equivalent. `Name` is deliberately **not** a common tag;
it is set per resource and overrides the inherited value. The loader rejects a config that
puts `Name` in `common.tags` or omits any of `Build-method`, `Environment`, `ManagedBy`,
`Owner`, `Compliance`.

## Local usage

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
npm install -g aws-cdk@2

pytest                                  # unit tests against the synthesized template
cdk synth  --context env=uat
cdk diff   --context env=uat            # the `terraform plan` equivalent
cdk deploy --all --context env=uat
```

Before the first deploy in each account/region:

```powershell
cdk bootstrap aws://<account-id>/<region>
```

## Adding a new "module"

1. Add a construct under `src/cognitech_cdk/constructs/` that takes `common: CommonProps`
   plus its own typed props and exposes the IDs other constructs need.
2. Add the matching schema + YAML keys in `src/cognitech_cdk/common/config.py` and each
   `deployments/<env>/env.yaml`.
3. Instantiate it from an existing stack, or create a new stack in `src/cognitech_cdk/stacks/`
   and register it in `app.py`.
4. Add assertions in `tests/unit/`.

See [DEPLOYMENT.md](DEPLOYMENT.md#6-where-the-remaining-constructs-go) for the planned
construct-to-stack mapping of the remaining Terraform modules.

Pass values between constructs as plain object references — CDK resolves them to
`Ref`/`Fn::GetAtt` automatically. Across stacks, pass the construct object; CDK creates
the CloudFormation export/import for you.

## GitHub Actions

Authentication uses **GitHub OIDC**, so no long-lived AWS keys are stored.

One-time AWS setup per account:

1. Create the OIDC provider for `token.actions.githubusercontent.com`.
2. Create two roles trusting that provider, with the subject condition scoped to this repo
   (e.g. `repo:<org>/cognitech-AWS-CDK-repo:ref:refs/heads/main` for deploy,
   `repo:<org>/cognitech-AWS-CDK-repo:pull_request` for plan):
   - a **plan** role: `ReadOnlyAccess` + `sts:AssumeRole` on `cdk-hnb659fds-lookup-role-*`
   - a **deploy** role: `sts:AssumeRole` on the `cdk-hnb659fds-*-role-*` bootstrap roles

GitHub configuration:

| Where | Name | Value |
| ----- | ---- | ----- |
| Repo secret                   | `AWS_PLAN_ROLE_ARN_UAT`   | plan role ARN (uat)     |
| Repo secret                   | `AWS_PLAN_ROLE_ARN_PROD`  | plan role ARN (prod)    |
| Environment `uat` / `prod`    | `AWS_DEPLOY_ROLE_ARN`     | deploy role ARN         |
| Repo variable                 | `AWS_REGION`              | e.g. `us-east-1`        |

Add required reviewers to the `prod` GitHub Environment to gate production deploys.

Workflows:

- `.github/workflows/cdk-pr.yml` — on PR: runs the tests once, works out which
  environments the PR affects, then runs `cdk diff` for each and comments the result.
- `.github/workflows/cdk-deploy.yml` — on push to `main` (or manual dispatch): deploys only
  the environments whose `deployments/<env>/` folder changed; a change under `src/` or
  `app.py` deploys all of them, in `deploy_order`, one at a time.
