#!/usr/bin/env bash
# Fail when the Helm chart embeds an older config schema than the root example.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE_YAML="$ROOT/config.example.yaml"
VALUES_YAML="$ROOT/deploy/helm/deer-flow/values.yaml"

for file in "$EXAMPLE_YAML" "$VALUES_YAML"; do
  if [ ! -f "$file" ]; then
    echo "::error::missing file: $file" >&2
    exit 1
  fi
done

example=$(grep -E '^config_version:[[:space:]]+[0-9]+' "$EXAMPLE_YAML" | head -1 | awk '{print $2}')
chart=$(awk '/^config:[[:space:]]*\|/{found=1; next} found && /^[[:space:]]+config_version:[[:space:]]+[0-9]+/ {print $2; exit}' "$VALUES_YAML")

printf 'config.example.yaml config_version=%s\n' "$example"
printf 'chart values.yaml     config_version=%s\n' "$chart"

if [ -z "$example" ] || [ -z "$chart" ]; then
  echo "::error::could not parse config_version from one of the files" >&2
  exit 1
fi
if [ "$chart" -ne "$example" ]; then
  echo "::error::chart config_version ($chart) must equal config.example.yaml ($example)." >&2
  exit 1
fi

echo "OK - chart config_version matches config.example.yaml."
