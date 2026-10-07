# cognitech-AWS-CDK-repo

AWS infrastructure built with the AWS CDK (Python).

One VPC per environment: public subnets and an internet gateway, plus optional private
subnets with NAT gateways and Elastic IPs. You pick how many availability zones.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full walkthrough and the pipeline setup.

## Layout

```
app.py                              entrypoint: picks an environment, builds the stack
cdk.json
pyproject.toml                      pytest config (adds src/ to the path)

src/cognitech_cdk/
  settings.py                       reads and validates env.yaml
  network_stack.py                  VPC, subnets, IGW, NAT gateways, Elastic IPs

deployments/                        one folder per environment
  uat/env.yaml
  prod/env.yaml

scripts/select_environments.py      feeds the GitHub Actions matrix
tests/unit/test_network_stack.py
.github/workflows/
```

`settings.py` turns one `env.yaml` into a `Settings` object; `network_stack.py` turns that
into a CloudFormation stack. That is the whole program.

## Defining a VPC

One flat file per environment, `deployments/<env>/env.yaml`:

```yaml
account_id: "111122223333"
region: us-east-1
deploy_order: 10

name_prefix: int-production-use1-cdk-uat

cidr: 10.20.0.0/16
azs: 2                 # 1, 2 or 3

private_subnets: true  # false = public subnets only, no NAT gateways
nat_gateways: 1        # omit for one per AZ

tags:
  Build-method: aws-cdk
  Environment: user-acceptance-test
  ManagedBy: aws-cdk/cognitech-AWS-CDK-repo
  Owner: kbrigthain@gmail.com
  Compliance: hippaa
```

| Key               | Default           | Meaning                                          |
| ----------------- | ----------------- | ------------------------------------------------ |
| `azs`             | `2`               | **1, 2 or 3** availability zones                 |
| `private_subnets` | `false`           | Add private subnets + NAT gateways + Elastic IPs |
| `nat_gateways`    | one per AZ        | Only used when `private_subnets: true`           |

### Public subnets only

Leave `private_subnets` out, or set it to `false`:

```yaml
cidr: 10.40.0.0/16
azs: 2
private_subnets: false
```

→ VPC, 2 public subnets, internet gateway, 2 route tables, 2 default routes.
**No NAT gateways, no Elastic IPs.**

### With private subnets

```yaml
private_subnets: true
nat_gateways: 1
```

→ the above, plus a private subnet per AZ, 1 NAT gateway, 1 Elastic IP, and a default route
from the private subnets to it. Omit `nat_gateways` for one per AZ. `private_subnets: true`
with `nat_gateways: 0` is rejected — those subnets would have no route out.

### Availability zones

`azs` is `1`, `2` or `3`. Zones are derived from `region` (`us-east-1a`, `-1b`, `-1c`) and
written straight into the template, so `cdk synth` and the tests need no AWS credentials.
Subnets are named by position: `primary`, `secondary`, `tertiary`.

## Naming and tagging

Every resource is named `{name_prefix}-...`:

| Resource         | Name                                           |
| ---------------- | ---------------------------------------------- |
| VPC              | `int-production-use1-cdk-uat-vpc`              |
| Internet gateway | `int-production-use1-cdk-uat-igw`              |
| Public subnet    | `int-production-use1-cdk-uat-public-primary`   |
| Private subnet   | `int-production-use1-cdk-uat-private-secondary`|
| Route table      | `int-production-use1-cdk-uat-public-primary-rtb` |
| NAT gateway      | `int-production-use1-cdk-uat-primary-natgw`    |
| Elastic IP       | `int-production-use1-cdk-uat-primary-nat-eip`  |

`tags` are applied once in the stack constructor with `Tags.of(self)`, which propagates to
every taggable resource. `Name` is not a common tag — it is set per resource. The loader
rejects a file that puts `Name` in `tags` or omits `Build-method`, `Environment`,
`ManagedBy`, `Owner` or `Compliance`.

## Local usage

`cdk.json` runs `python app.py`, so **always work inside an activated virtual environment**.
A venv built on Windows cannot be used from WSL and vice versa — keep one per platform.

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
pytest                              # asserts against the synthesized template
cdk ls     --context env=uat
cdk synth  --context env=uat
cdk diff   --context env=uat        # what a deploy would change
cdk deploy --context env=uat
```

First deploy in an account/region needs `cdk bootstrap aws://<account-id>/<region>`.

If `cdk` reports `/bin/sh: 1: python: not found` (exit code 127), the venv is not
activated — Ubuntu has no `python`, only `python3`. See
[DEPLOYMENT.md](DEPLOYMENT.md#31-local-toolchain).

## Adding an environment

Copy `deployments/uat/` to `deployments/<name>/` and edit it. It is discovered
automatically, deployable with `--context env=<name>`, and picked up by the pipelines with
no workflow edits. `deploy_order` controls promotion order.

## Adding more resources

1. Add a field to `Settings` in `src/cognitech_cdk/settings.py` and the key to each
   `deployments/<env>/env.yaml`.
2. Add the resource to `NetworkStack` in `src/cognitech_cdk/network_stack.py`, naming it
   `f"{settings.name_prefix}-..."`. Don't re-apply the tags — the stack already tags the
   whole tree.
3. For a different lifecycle (databases, compute), add a module next to `network_stack.py`
   and register it in `app.py`.
4. Add assertions in `tests/unit/test_network_stack.py`.

The VPC is a normal `ec2.IVpc`, so anything downstream can use `stack.vpc`,
`stack.vpc.public_subnets` and `stack.vpc.private_subnets` directly.

## GitHub Actions

Authentication uses GitHub OIDC; no long-lived AWS keys are stored.

| Where                      | Name                     | Value               |
| -------------------------- | ------------------------ | ------------------- |
| Repo secret                | `AWS_PLAN_ROLE_ARN_UAT`  | plan role ARN (uat) |
| Repo secret                | `AWS_PLAN_ROLE_ARN_PROD` | plan role ARN (prod)|
| Environment `uat` / `prod` | `AWS_DEPLOY_ROLE_ARN`    | deploy role ARN     |
| Repo variable              | `AWS_REGION`             | e.g. `us-east-1`    |

Run `./scripts/create_github_oidc_roles.sh` once per account to create the provider and
both roles. Add required reviewers to the `prod` GitHub Environment to gate production.

- `.github/workflows/cdk-pr.yml` — on PR: runs the tests, works out which environments the
  PR affects, runs `cdk diff` for each and comments the result.
- `.github/workflows/cdk-deploy.yml` — on push to `main` or manual dispatch: deploys only
  the environments whose `deployments/<env>/` folder changed; a change under `src/` or
  `app.py` deploys all of them, in `deploy_order`, one at a time.
