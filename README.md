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
  constructs/
    network.py                          Network: VPC, IGW, subnets, NAT gateways, EIPs
  stacks/
    network_stack.py                    NetworkStack: tags, construct, outputs

deployments/
  dev/env.yaml                          one folder per environment
  prod/env.yaml

tests/unit/
.github/workflows/
  deploy-primary-mdpp-dev.yml           deploy dev into md-preproduction
  pr.yml                                tests + synth on every PR
scripts/create_github_oidc_roles.sh     one-time AWS setup for the pipeline
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

---

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
| `github-oidc-cdk-plan-<env>` | `ReadOnlyAccess` for a PR-time `cdk diff` |

Verify:

```bash
aws iam get-role --role-name github-oidc-cdk-deploy-dev \
  --profile admin-mdpp --query 'Role.Arn' --output text
# arn:aws:iam::533267408704:role/github-oidc-cdk-deploy-dev
```

Copy that ARN — it goes straight into the workflow in Step 4.

> The script finishes by printing an `AWS_PLAN_ROLE_ARN_<ENV>` secret to add.
> **The current `pr.yml` does not use it** — pull requests only run tests and
> `cdk synth`, which need no AWS access. The plan role is harmless; skip that
> secret unless you later add a credentialed `cdk diff` to the PR workflow.

#### Step 3 — Create the GitHub Environment

Settings → Environments → **New environment**, named **exactly** like the folder
under `deployments/`: `dev`.

The name must match. The workflow sets `environment: dev`, and the OIDC trust
policy expects the claim
`repo:KahBrightTech/cognitech-AWS-CDK-repo:environment:dev`. A mismatch fails
with `Not authorized to perform sts:AssumeRoleWithWebIdentity`.

This is also where **Required reviewers** live — add them on any environment
that should pause for approval before deploying.

The Environment needs no secrets. Creating it is the only GitHub configuration
step.

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
> expects the claim `repo:<org>/<repo>:environment:dev`. The *file name* and the
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

Suggested stack grouping, roughly one per lifecycle:

| Stack                 | Holds                                                    |
| --------------------- | -------------------------------------------------------- |
| `network_stack.py`  | VPC, subnets, VPC endpoints, transit gateway attachments |
| `security_stack.py` | security groups, WAF                                     |
| `platform_stack.py` | IAM roles and policies, SSM parameters, secrets, ECR     |
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
| `common.tags is missing ...`                                                     | Add the required tag keys.                                               |
| `Need to perform AWS calls for account X, but the current credentials are for Y` | `account_id` does not match your profile.                              |
| `ExpiredToken` / `InvalidClientTokenId`                                        | `aws sso login --profile <your-profile>`                               |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity`                        | GitHub Environment name does not match the folder under`deployments/`. |
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
