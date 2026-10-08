#!/usr/bin/env bash
#
# Create the GitHub OIDC provider and the role the deploy workflow assumes.
#
# Run with no arguments to be prompted for each value, with defaults discovered
# from your AWS profile, the git remote and the deployments/ folder. Pass flags
# to skip the prompts.
#
# Idempotent: re-running updates the trust and permission policies in place and
# never deletes anything.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ACCOUNT_ID=""
ENVIRONMENT=""
REPO=""
OWNER_ID=""
REPO_ID=""
REGION=""
PROFILE="${AWS_PROFILE:-}"
QUALIFIER="hnb659fds"
ASSUME_YES=false

usage() {
  cat <<'TXT'
Usage: scripts/create_github_oidc_roles.sh [options]

Creates the GitHub OIDC provider and the IAM role used by the deploy workflow.
With no options it prompts for each value, offering a discovered default.

Options:
  --account-id <id>      AWS account to create the roles in
  --environment <name>   Environment name, matching a folder in deployments/
  --repo <org/repo>      GitHub repository allowed to assume the roles
  --owner-id <id>        Numeric GitHub owner ID (discovered from the API)
  --repo-id <id>         Numeric GitHub repository ID (discovered from the API)
  --region <region>      Region the CDK bootstrap roles live in
  --profile <name>       AWS CLI profile to use
  --qualifier <string>   CDK bootstrap qualifier (default: hnb659fds)
  -y, --yes              Never prompt; fail if a required value is missing
  -h, --help             Show this message

Example:
  scripts/create_github_oidc_roles.sh \
    --account-id 533267408704 \
    --environment uat \
    --repo KahBrightTech/cognitech-AWS-CDK-repo \
    --region us-east-1 \
    --profile admin-mdpp
TXT
  exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account-id)  ACCOUNT_ID="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --repo)        REPO="$2"; shift 2 ;;
    --owner-id)    OWNER_ID="$2"; shift 2 ;;
    --repo-id)     REPO_ID="$2"; shift 2 ;;
    --region)      REGION="$2"; shift 2 ;;
    --profile)     PROFILE="$2"; shift 2 ;;
    --qualifier)   QUALIFIER="$2"; shift 2 ;;
    -y|--yes)      ASSUME_YES=true; shift ;;
    -h|--help)     usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

# ------------------------------------------------------------------- discovery
aws_cli() {
  if [[ -n "$PROFILE" ]]; then aws --profile "$PROFILE" "$@"; else aws "$@"; fi
}

discover_account() {
  aws_cli sts get-caller-identity --query Account --output text 2>/dev/null || true
}

discover_region() {
  local found
  found="$(aws_cli configure get region 2>/dev/null || true)"
  echo "${found:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}"
}

discover_repo() {
  local url
  url="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  [[ -z "$url" ]] && return 0
  url="${url%.git}"
  url="${url#git@github.com:}"
  url="${url#ssh://git@github.com/}"
  url="${url#https://github.com/}"
  echo "$url"
}

discover_environments() {
  local path
  for path in "$REPO_ROOT"/deployments/*/env.yaml; do
    [[ -e "$path" ]] || continue
    basename "$(dirname "$path")"
  done
}

# GitHub now issues OIDC subjects that carry the immutable numeric owner and
# repository IDs, e.g. repo:org@202037050/repo@1383489754:environment:dev. Names
# can be recycled, IDs cannot. Both forms are trusted so the roles keep working
# whichever one GitHub sends.
discover_github_ids() { # $1 = org/repo -> prints "<owner_id> <repo_id>"
  local json=""
  # `gh` first: it carries the user's credentials, so private repos resolve too.
  if command -v gh >/dev/null 2>&1; then
    json="$(gh api "repos/$1" 2>/dev/null || true)"
  fi
  if [[ -z "$json" ]] && command -v curl >/dev/null 2>&1; then
    json="$(curl -fsSL -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$1" 2>/dev/null || true)"
  fi
  [[ -n "$json" ]] || return 1
  printf '%s' "$json" |
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["owner"]["id"], d["id"])' \
    2>/dev/null || return 1
}

prompt() { # $1 = label, $2 = default, $3 = variable to set
  local label="$1" default="$2" varname="$3" answer=""
  if [[ -n "$default" ]]; then
    read -r -p "${label} [${default}]: " answer
    answer="${answer:-$default}"
  else
    while [[ -z "$answer" ]]; do read -r -p "${label}: " answer; done
  fi
  printf -v "$varname" '%s' "$answer"
}

# ----------------------------------------------------------------- interactive
if [[ "$ASSUME_YES" == false && -t 0 ]]; then
  echo "Press Enter to accept the value in brackets."
  echo

  [[ -z "$PROFILE" ]] && prompt "AWS profile" "default" PROFILE
  [[ -z "$REGION" ]] && prompt "Region" "$(discover_region)" REGION
  [[ -z "$ACCOUNT_ID" ]] && prompt "Account ID" "$(discover_account)" ACCOUNT_ID
  [[ -z "$REPO" ]] && prompt "GitHub repository (org/name)" "$(discover_repo)" REPO

  if [[ -z "$ENVIRONMENT" ]]; then
    known="$(discover_environments || true)"
    [[ -n "$known" ]] && echo "Environments in deployments/: $(echo "$known" | tr '\n' ' ')"
    # Deliberately no default: picking the wrong environment targets the wrong account.
    prompt "Environment" "" ENVIRONMENT
  fi
  echo
fi

REGION="${REGION:-us-east-1}"
if [[ -z "$ACCOUNT_ID" || -z "$ENVIRONMENT" || -z "$REPO" ]]; then
  echo "Missing --account-id, --environment or --repo." >&2
  usage
fi

AWS=(aws)
if [[ -n "$PROFILE" ]]; then AWS+=(--profile "$PROFILE"); fi

PROVIDER_HOST="token.actions.githubusercontent.com"
PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${PROVIDER_HOST}"
DEPLOY_ROLE="github-oidc-cdk-deploy-${ENVIRONMENT}"
BOOTSTRAP_ROLES="arn:aws:iam::${ACCOUNT_ID}:role/cdk-${QUALIFIER}-*-role-${ACCOUNT_ID}-${REGION}"

if [[ -z "$OWNER_ID" || -z "$REPO_ID" ]]; then
  if ids="$(discover_github_ids "$REPO")"; then
    read -r OWNER_ID REPO_ID <<<"$ids"
  fi
fi

# Both the name-based and the ID-based subject, so the roles work before and
# after GitHub switches this repository over.
REPO_SUBJECTS=("$REPO")
if [[ -n "$OWNER_ID" && -n "$REPO_ID" ]]; then
  REPO_SUBJECTS+=("${REPO%%/*}@${OWNER_ID}/${REPO#*/}@${REPO_ID}")
fi

cat <<SUMMARY
Profile     : ${PROFILE:-<default>}
Account     : ${ACCOUNT_ID}
Region      : ${REGION}
Environment : ${ENVIRONMENT}
Repository  : ${REPO}
Repo IDs    : ${OWNER_ID:-<unresolved>}/${REPO_ID:-<unresolved>}
Qualifier   : ${QUALIFIER}

Will create or update:
  OIDC provider  ${PROVIDER_HOST}
  IAM role       ${DEPLOY_ROLE}

SUMMARY

if [[ -z "$OWNER_ID" || -z "$REPO_ID" ]]; then
  echo "[warn] Could not read the numeric GitHub IDs for ${REPO}; only the" >&2
  echo "       name-based subject will be trusted. If the workflow then fails" >&2
  echo "       with 'Not authorized to perform sts:AssumeRoleWithWebIdentity'," >&2
  echo "       re-run with --owner-id and --repo-id." >&2
  echo >&2
fi

if [[ "$ASSUME_YES" == false && -t 0 ]]; then
  read -r -p "Proceed? [y/N]: " reply
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]] || { echo "Aborted; nothing changed."; exit 0; }
  echo
fi

caller_account=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
if [[ "$caller_account" != "$ACCOUNT_ID" ]]; then
  echo "Refusing to continue: credentials are for ${caller_account}, not ${ACCOUNT_ID}." >&2
  exit 1
fi

if "${AWS[@]}" iam get-role --role-name "cdk-${QUALIFIER}-deploy-role-${ACCOUNT_ID}-${REGION}" >/dev/null 2>&1; then
  echo "[ok]   CDK bootstrap roles found."
else
  echo "Run 'cdk bootstrap aws://${ACCOUNT_ID}/${REGION}' first." >&2
  exit 1
fi

# ---------------------------------------------------------------- OIDC provider
if "${AWS[@]}" iam get-open-id-connect-provider \
     --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "[skip] OIDC provider already exists."
else
  # IAM no longer validates the thumbprint for this issuer, but the API still requires one.
  "${AWS[@]}" iam create-open-id-connect-provider \
    --url "https://${PROVIDER_HOST}" \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null
  echo "[new]  OIDC provider created."
fi

# ----------------------------------------------------------------------- helpers
trust_policy() { # $1 = sub claim suffix, e.g. "environment:dev"
  local subs="" repo
  for repo in "${REPO_SUBJECTS[@]}"; do
    subs+="${subs:+,
          }\"repo:${repo}:$1\""
  done
  cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "${PROVIDER_ARN}" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "${PROVIDER_HOST}:aud": "sts.amazonaws.com",
        "${PROVIDER_HOST}:sub": [
          ${subs}
        ]
      }
    }
  }]
}
JSON
}

upsert_role() { # $1 = role name, $2 = trust policy, $3 = description
  if "${AWS[@]}" iam get-role --role-name "$1" >/dev/null 2>&1; then
    "${AWS[@]}" iam update-assume-role-policy --role-name "$1" --policy-document "$2"
    echo "[upd]  Role ${1} trust policy updated."
  else
    "${AWS[@]}" iam create-role --role-name "$1" \
      --assume-role-policy-document "$2" \
      --description "$3" \
      --max-session-duration 3600 >/dev/null
    echo "[new]  Role ${1} created."
  fi
}

# ------------------------------------------------------------------ deploy role
# The deploy job sets `environment:`, so GitHub issues sub=repo:<repo>:environment:<name>.
upsert_role "$DEPLOY_ROLE" \
  "$(trust_policy "environment:${ENVIRONMENT}")" \
  "GitHub Actions CDK deploy for ${ENVIRONMENT}"

"${AWS[@]}" iam put-role-policy --role-name "$DEPLOY_ROLE" \
  --policy-name assume-cdk-bootstrap-roles \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "${BOOTSTRAP_ROLES}"
  }]
}
JSON
)"
echo "[ok]   ${DEPLOY_ROLE} may assume ${BOOTSTRAP_ROLES}"

echo
echo "Add these to GitHub:"
echo "  Environment '${ENVIRONMENT}' secret  AWS_DEPLOY_ROLE_ARN = arn:aws:iam::${ACCOUNT_ID}:role/${DEPLOY_ROLE}"
echo "  Repository variable                  AWS_REGION          = ${REGION}"
