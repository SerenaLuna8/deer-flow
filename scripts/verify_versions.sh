#!/usr/bin/env bash
# Verify that all application package versions agree and the lock is current.
#
# Sources checked:
#   backend/pyproject.toml             — version
#   backend/packages/harness/pyproject.toml — version
#   frontend/package.json              — version
#   backend/uv.lock                    — freshness via uv lock --check
#
# Usage:
#   scripts/verify_versions.sh             # all sources must be mutually equal
#   scripts/verify_versions.sh 2.1.0       # all sources must equal 2.1.0
#
# Exit status is 0 when consistent and 1 otherwise. This is a local release
# check for the private project.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYPROJECT="$ROOT/backend/pyproject.toml"
HARNESS_PYPROJECT="$ROOT/backend/packages/harness/pyproject.toml"
PACKAGE="$ROOT/frontend/package.json"

for f in "$PYPROJECT" "$HARNESS_PYPROJECT" "$PACKAGE"; do
  if [ ! -f "$f" ]; then
    echo "error: missing version file: $f" >&2
    exit 1
  fi
done

PY_VERSION=$(awk -F'"' '/^version[[:space:]]*=/ {print $2; exit}' "$PYPROJECT")
HARNESS_VERSION=$(awk -F'"' '/^version[[:space:]]*=/ {print $2; exit}' "$HARNESS_PYPROJECT")
JS_VERSION=$(grep -m1 '"version"' "$PACKAGE" | awk -F'"' '{print $4}')

printf 'backend/pyproject.toml: %s\n' "$PY_VERSION"
printf 'harness/pyproject.toml: %s\n' "$HARNESS_VERSION"
printf 'frontend/package.json:  %s\n' "$JS_VERSION"

if [ -z "$PY_VERSION" ] || [ -z "$HARNESS_VERSION" ] || [ -z "$JS_VERSION" ]; then
  echo "error: could not parse one or more project versions" >&2
  exit 1
fi

# mismatch <name> <actual> <expected>: returns 1 when values differ.
mismatch() {
  if [ "$2" != "$3" ]; then
    echo "error: $1 is '$2' but expected '$3'." >&2
    return 1
  fi
  return 0
}

EXPECTED="${1:-}"
status=0

if [ -n "$EXPECTED" ]; then
  printf 'Expected:               %s (from tag v%s)\n\n' "$EXPECTED" "$EXPECTED"
  mismatch "backend/pyproject.toml" "$PY_VERSION"    "$EXPECTED" || status=1
  mismatch "harness/pyproject.toml" "$HARNESS_VERSION" "$EXPECTED" || status=1
  mismatch "frontend/package.json"  "$JS_VERSION"    "$EXPECTED" || status=1
else
  echo
  mismatch "harness/pyproject.toml" "$HARNESS_VERSION" "$PY_VERSION" || status=1
  mismatch "frontend/package.json" "$JS_VERSION" "$PY_VERSION" || status=1
fi

if [ "$status" -ne 0 ]; then
  if [ -n "$EXPECTED" ]; then
    echo "Tip: run scripts/bump_version.sh $EXPECTED to align all sources." >&2
  else
    echo "Tip: run scripts/bump_version.sh <version> to align all sources." >&2
  fi
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required to verify backend/uv.lock" >&2
  exit 1
fi
if ! (cd "$ROOT/backend" && uv lock --check); then
  echo "error: backend/uv.lock is not current" >&2
  exit 1
fi

echo "OK — application versions agree on ${PY_VERSION} and backend/uv.lock is current."
