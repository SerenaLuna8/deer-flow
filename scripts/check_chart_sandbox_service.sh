#!/usr/bin/env bash
# Assert that the chart keeps sandbox Services private unless NodePort is opted in.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART="$ROOT/deploy/helm/deer-flow"

if ! command -v helm >/dev/null 2>&1; then
  echo "::error::helm is required to run this check" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
DATABASE_ARGS=(
  --set-string
  "postgresql.external.databaseUrl=postgresql://postgres:test@db:5432/deerflow"
)

helm template deer-flow "$CHART" --include-crds "${DATABASE_ARGS[@]}" >"$TMP/default.yaml" || exit 1
helm template deer-flow "$CHART" --include-crds "${DATABASE_ARGS[@]}" \
  --set provisioner.sandboxServiceType=NodePort >"$TMP/nodeport.yaml" || exit 1
helm template deer-flow "$CHART" --include-crds "${DATABASE_ARGS[@]}" \
  --set provisioner.sandboxServiceType=NodePort \
  --set provisioner.nodeHost=192.168.1.10 >"$TMP/nodeport-host.yaml" || exit 1

has_env() { grep -qE "^[[:space:]]*- name: $1\$" "$2"; }
env_value() {
  grep -A1 -E "^[[:space:]]*- name: $1\$" "$2" |
    grep -oE 'value: "[^"]*"' |
    head -1
}

errors=0
check() {
  if [ "$1" -eq 0 ]; then
    echo "PASS  $2"
  else
    echo "FAIL  $2"
    errors=$((errors + 1))
  fi
}

has_env SANDBOX_SERVICE_TYPE "$TMP/default.yaml"; check $? "default service type env exists"
[ "$(env_value SANDBOX_SERVICE_TYPE "$TMP/default.yaml")" = 'value: "ClusterIP"' ]; check $? "default service type is ClusterIP"
if has_env NODE_HOST "$TMP/default.yaml"; then check 1 "default has no NODE_HOST"; else check 0 "default has no NODE_HOST"; fi

has_env SANDBOX_SERVICE_TYPE "$TMP/nodeport.yaml"; check $? "NodePort service type env exists"
[ "$(env_value SANDBOX_SERVICE_TYPE "$TMP/nodeport.yaml")" = 'value: "NodePort"' ]; check $? "NodePort override is honored"
has_env NODE_HOST "$TMP/nodeport.yaml"; check $? "NodePort receives NODE_HOST"

[ "$(env_value NODE_HOST "$TMP/nodeport-host.yaml")" = 'value: "192.168.1.10"' ]; check $? "explicit nodeHost is honored"

if [ "$errors" -ne 0 ]; then
  echo "::error::$errors sandbox Service assertion(s) failed" >&2
  exit 1
fi
echo "All sandbox Service assertions passed."
