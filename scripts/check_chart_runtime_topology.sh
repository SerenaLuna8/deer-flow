#!/usr/bin/env bash
# Guard the final process ownership rendered by the Helm chart.

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
  --set scheduler.enabled=true >"$TMP/scheduler.yaml" || exit 1

require() {
  if ! grep -qF "$1" "$2"; then
    echo "::error::$3" >&2
    exit 1
  fi
}
reject() {
  if grep -qF "$1" "$2"; then
    echo "::error::$3" >&2
    exit 1
  fi
}

require "# Source: deer-flow/templates/gateway-deployment.yaml" "$TMP/default.yaml" "Gateway Deployment did not render"
require "# Source: deer-flow/templates/worker-deployment.yaml" "$TMP/default.yaml" "Worker Deployment did not render"
reject "# Source: deer-flow/templates/scheduler-deployment.yaml" "$TMP/default.yaml" "Scheduler rendered while disabled"
require "# Source: deer-flow/templates/scheduler-deployment.yaml" "$TMP/scheduler.yaml" "Scheduler did not render when enabled"

require "app.worker.app" "$TMP/default.yaml" "Worker command does not own graph execution"
reject "app.worker.app:app" "$TMP/default.yaml" "Gateway must not expose the Worker as an ASGI app"
require "app.scheduler.app" "$TMP/scheduler.yaml" "Scheduler command is missing"

for key in \
  AUTH_JWT_SECRET \
  DEER_FLOW_AUDIT_KEYRING_JSON \
  DEER_FLOW_CREDENTIAL_KEYRING_JSON \
  DEER_FLOW_INTERNAL_AUTH_TOKEN \
  DEER_FLOW_PROXY_AUTH_TOKEN \
  PROVISIONER_API_KEY; do
  require "$key" "$TMP/default.yaml" "required runtime secret $key is not wired"
done

echo "Helm runtime topology and secret wiring assertions passed."
