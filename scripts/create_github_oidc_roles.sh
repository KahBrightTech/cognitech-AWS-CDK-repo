#!/usr/bin/env bash
#
# Create the GitHub OIDC provider and the two roles the workflows assume.
#
# Idempotent: re-running updates the trust and permission policies in place and
# never deletes anything.
#
#   ./scripts/create_github_oidc_roles.sh \
#       --account-id 533267408704 \
#       --environment uat \
#       --repo KahBrightTech/cognitech-AWS-CDK-repo \
#       --region us-east-1 \
#       --profile admin-mdpp
#
set -euo pipefail

ACCOUNT_ID=""
ENVIRONMENT=""
REPO=""
REGION="us-east-1"
PROFILE=""
QUALIFIER="hnb659fds"

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account-id)  ACCOUNT_ID="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --repo)        REPO="$2"; shift 2 ;;
    --region)      REGION="$2"; shift 2 ;;
    --profile)     PROFILE="$2"; shift 2 ;;
    --qualifier)   QUALIFIER="$2"; shift 2 ;;
    -h|--help)     usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n "$ACCOUNT_ID" && -n "$ENVIRONMENT" && -n "$REPO" ]] || usage

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

PROVIDER_HOST="token.actions.githubusercontent.com"
PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${PROVIDER_HOST}"
DEPLOY_ROLE="github-oidc-cdk-deploy-${ENVIRONMENT}"
PLAN_ROLE="github-oidc-cdk-plan-${ENVIRONMENT}"
BOOTSTRAP_ROLES="arn:aws:iam::${ACCOUNT_ID}:role/cdk-${QUALIFIER}-*-role-${ACCOUNT_ID}-${REGION}"
LOOKUP_ROLE="arn:aws:iam::${ACCOUNT_ID}:role/cdk-${QUALIFIER}-lookup-role-${ACCOUNT_ID}-${REGION}"

echo "Account     : ${ACCOUNT_ID}"
echo "Region      : ${REGION}"
echo "Environment : ${ENVIRONMENT}"
echo "Repository  : ${REPO}"
echo

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
trust_policy() { # $1 = sub claim
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
        "${PROVIDER_HOST}:sub": "$1"
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
  "$(trust_policy "repo:${REPO}:environment:${ENVIRONMENT}")" \
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

# -------------------------------------------------------------------- plan role
# The PR job has no `environment:`, so sub=repo:<repo>:pull_request.
upsert_role "$PLAN_ROLE" \
  "$(trust_policy "repo:${REPO}:pull_request")" \
  "GitHub Actions CDK diff for ${ENVIRONMENT}"

"${AWS[@]}" iam attach-role-policy --role-name "$PLAN_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess

"${AWS[@]}" iam put-role-policy --role-name "$PLAN_ROLE" \
  --policy-name assume-cdk-lookup-role \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "${LOOKUP_ROLE}"
  }]
}
JSON
)"
echo "[ok]   ${PLAN_ROLE} has ReadOnlyAccess and may assume the lookup role"

echo
echo "Add these to GitHub:"
echo "  Environment '${ENVIRONMENT}' secret  AWS_DEPLOY_ROLE_ARN       = arn:aws:iam::${ACCOUNT_ID}:role/${DEPLOY_ROLE}"
echo "  Repository secret                    AWS_PLAN_ROLE_ARN_${ENVIRONMENT^^} = arn:aws:iam::${ACCOUNT_ID}:role/${PLAN_ROLE}"
echo "  Repository variable                  AWS_REGION                = ${REGION}"
