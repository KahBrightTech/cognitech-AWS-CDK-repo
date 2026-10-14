#!/usr/bin/env bash
# Throwaway harness mirroring the "Select stacks" step.
cdk() { printf '%s\n' "md-preproduction-use1-dev-network" "md-preproduction-use1-dev-data" ""; }

run() {
  REQUESTED="$1"
  ENV=dev
  GITHUB_ENV=$(mktemp)
  GITHUB_STEP_SUMMARY=$(mktemp)
  (
    set -euo pipefail
    mapfile -t available < <(cdk list | sed '/^$/d')
    if [ "${#available[@]}" -eq 0 ]; then
      echo "::error::cdk list returned no stacks for ENV=$ENV"
      exit 1
    fi

    {
      echo "### Stacks detected in \`dev\`"
      echo
      printf -- '- `%s`\n' "${available[@]}"
      echo
    } >> "$GITHUB_STEP_SUMMARY"

    requested="${REQUESTED:-all}"
    [ -n "${requested// /}" ] || requested=all

    if [ "$requested" = "all" ]; then
      echo "TARGETS=--all" >> "$GITHUB_ENV"
      echo "SELECTED=all (${#available[@]} stack(s))" >> "$GITHUB_ENV"
      exit 0
    fi

    targets=()
    for want in $requested; do
      matched=()
      for have in "${available[@]}"; do
        case "$have" in $want) matched+=("$have") ;; esac
      done
      if [ "${#matched[@]}" -eq 0 ]; then
        echo "::error::No stack matches '$want'. Available: ${available[*]}"
        exit 1
      fi
      targets+=("$want")
    done

    echo "TARGETS=${targets[*]}" >> "$GITHUB_ENV"
    echo "SELECTED=${targets[*]}" >> "$GITHUB_ENV"
  )
  echo "input=[$1] exit=$? -> $(tr '\n' ' ' < "$GITHUB_ENV")"
}

run ""
run "all"
run "md-preproduction-use1-dev-network"
run "*network*"
run "md-preproduction-use1-dev-network md-preproduction-use1-dev-data"
run "typo-stack"
