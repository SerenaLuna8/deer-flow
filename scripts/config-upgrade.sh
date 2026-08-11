#!/usr/bin/env bash
#
# config-upgrade.sh - Upgrade config.yaml to match config.example.yaml
#
# 1. Runs version-specific migrations (value replacements, renames, etc.)
# 2. Merges missing fields from the example into the user config
# 3. Backs up config.yaml to config.yaml.bak before modifying.

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="$REPO_ROOT/config.example.yaml"

# Resolve config.yaml location: canonical environment path > compatibility
# environment path > repository root.  Dual aliases must agree after
# normalization so an upgrade can never modify an unintended file.
_normalize_config_path() {
    local raw_path=$1
    local path_dir
    if ! path_dir="$(cd "$(dirname "$raw_path")" 2>/dev/null && pwd -P)"; then
        echo "✗ Config path parent directory does not exist: $raw_path" >&2
        return 1
    fi
    printf '%s/%s\n' "$path_dir" "$(basename "$raw_path")"
}

ACT_CONFIG=""
LEGACY_CONFIG=""
if [ -n "${ACT_WEAVE_CONFIG_PATH:-}" ]; then
    ACT_CONFIG="$(_normalize_config_path "$ACT_WEAVE_CONFIG_PATH")"
fi
if [ -n "${DEER_FLOW_CONFIG_PATH:-}" ]; then
    LEGACY_CONFIG="$(_normalize_config_path "$DEER_FLOW_CONFIG_PATH")"
fi
if [ -n "$ACT_CONFIG" ] && [ -n "$LEGACY_CONFIG" ] && [ "$ACT_CONFIG" != "$LEGACY_CONFIG" ]; then
    echo "✗ ACT_WEAVE_CONFIG_PATH resolves to '$ACT_CONFIG', but DEER_FLOW_CONFIG_PATH resolves to '$LEGACY_CONFIG'." >&2
    echo "  Refusing conflicting config paths." >&2
    exit 1
fi

if [ -n "$ACT_CONFIG" ] || [ -n "$LEGACY_CONFIG" ]; then
    CONFIG="${ACT_CONFIG:-$LEGACY_CONFIG}"
    if [ ! -f "$CONFIG" ]; then
        echo "✗ ACT_WEAVE_CONFIG_PATH/DEER_FLOW_CONFIG_PATH does not name a file: $CONFIG"
        exit 1
    fi
else
    CONFIG="$REPO_ROOT/config.yaml"
fi
export ACT_WEAVE_CONFIG_PATH="$CONFIG"
export DEER_FLOW_CONFIG_PATH="$CONFIG"

if [ ! -f "$EXAMPLE" ]; then
    echo "✗ config.example.yaml not found at $EXAMPLE"
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    echo "No config.yaml found — creating from example..."
    cp "$EXAMPLE" "$REPO_ROOT/config.yaml"
    echo "OK config.yaml created. Review the process settings, then configure models and Credentials at /admin/settings/models after startup."
    exit 0
fi

# Use inline Python to do migrations + recursive merge with PyYAML
if command -v cygpath >/dev/null 2>&1; then
    CONFIG_WIN="$(cygpath -w "$CONFIG")"
    EXAMPLE_WIN="$(cygpath -w "$EXAMPLE")"
else
    CONFIG_WIN="$CONFIG"
    EXAMPLE_WIN="$EXAMPLE"
fi

cd "$REPO_ROOT/backend" && CONFIG_WIN_PATH="$CONFIG_WIN" EXAMPLE_WIN_PATH="$EXAMPLE_WIN" uv run python -c "
import os
import sys, shutil, copy, re
from pathlib import Path

import yaml
from deerflow.config.app_config import DATABASE_RUNTIME_YAML_PATH_TOMBSTONES

config_path = Path(os.environ['CONFIG_WIN_PATH'])
example_path = Path(os.environ['EXAMPLE_WIN_PATH'])

with open(config_path, encoding='utf-8') as f:
    raw_text = f.read()
    user = yaml.safe_load(raw_text) or {}

with open(example_path, encoding='utf-8') as f:
    example = yaml.safe_load(f) or {}

user_version = user.get('config_version', 0)
example_version = example.get('config_version', 0)

if user_version >= example_version:
    print(f'OK config.yaml is already up to date (version {user_version}).')
    sys.exit(0)

print(f'Upgrading config.yaml: version {user_version} -> {example_version}')
print()

# ── Migrations ───────────────────────────────────────────────────────────
# Each migration targets a specific version upgrade.
# 'replacements': list of (old_string, new_string) applied to the raw YAML text.
#   This handles value changes that a dict merge cannot catch.
# 'remove_keys': removed top-level application sections to delete from old configs.
# 'remove_paths': removed nested application fields to delete from old configs.

MIGRATIONS = {
    1: {
        'description': 'Rename src.* module paths to deerflow.*',
        'replacements': [
            ('src.community.', 'deerflow.community.'),
            ('src.sandbox.', 'deerflow.sandbox.'),
            ('src.models.', 'deerflow.models.'),
            ('src.tools.', 'deerflow.tools.'),
        ],
    },
    24: {
        'description': 'Remove the retired database backup and restore configuration',
        'remove_keys': ['recovery'],
    },
    25: {
        'description': 'Remove configuration fields no longer consumed by the project-first runtime',
        'remove_keys': ['skill_evolution', 'skill_scan'],
        'remove_paths': [
            'uploads.max_files',
            'uploads.max_file_size',
            'uploads.max_total_size',
            'uploads.auto_convert_documents',
            'scheduler.lease_seconds',
            'worker.default_max_attempts',
            'quotas.max_member_limit',
            'quotas.max_storage_bytes_limit',
            'quotas.max_concurrent_run_limit',
            'quotas.max_mcp_calls_daily_limit',
        ],
    },
    32: {
        'description': 'Reject the unsupported legacy generic authorization provider configuration',
        'remove_keys': ['authorization'],
    },
    33: {
        'description': 'Move model configuration to PostgreSQL-backed system settings',
        'remove_keys': ['models'],
    },
    34: {
        'description': 'Move live agent, registration and quota policy leaves to PostgreSQL system settings',
        'remove_paths': sorted(DATABASE_RUNTIME_YAML_PATH_TOMBSTONES),
    },
    35: {
        'description': 'Replace exact project MCP endpoints with bounded CIDR network policy',
        'migrate_mcp_endpoint_policy': True,
    },
    # Future migrations go here:
    # 2: {
    #     'description': '...',
    #     'replacements': [('old', 'new')],
    # },
}

# Apply migrations in order for versions (user_version, example_version]
migrated = []
keys_to_remove = []
paths_to_remove = []
migrate_mcp_endpoint_policy = False
for version in range(user_version + 1, example_version + 1):
    migration = MIGRATIONS.get(version)
    if not migration:
        continue
    desc = migration.get('description', f'Migration to v{version}')
    for old, new in migration.get('replacements', []):
        if old in raw_text:
            raw_text = raw_text.replace(old, new)
            migrated.append(f'{old} -> {new}')
    keys_to_remove.extend(migration.get('remove_keys', []))
    paths_to_remove.extend(migration.get('remove_paths', []))
    migrate_mcp_endpoint_policy = migrate_mcp_endpoint_policy or migration.get('migrate_mcp_endpoint_policy', False)

# Re-parse after text migrations
user = yaml.safe_load(raw_text) or {}
if migrate_mcp_endpoint_policy:
    if 'mcp_security' not in user:
        user['mcp_security'] = {}
    mcp_security = user['mcp_security']
    if not isinstance(mcp_security, dict):
        print('✗ Cannot migrate mcp_security because the existing value is not a mapping.')
        print('  Configure mcp_security.project_remote_allowed_networks explicitly.')
        print('  No files were changed.')
        sys.exit(1)
    if 'project_remote_allowed_endpoints' in mcp_security:
        retired_endpoints = mcp_security['project_remote_allowed_endpoints']
        if retired_endpoints != []:
            print('✗ Cannot migrate nonempty mcp_security.project_remote_allowed_endpoints to CIDR policy automatically.')
            print('  Remove the retired endpoint list and configure mcp_security.project_remote_allowed_networks explicitly.')
            print('  No files were changed.')
            sys.exit(1)
        mcp_security.pop('project_remote_allowed_endpoints')
        migrated.append('mcp_security.project_remote_allowed_endpoints=[] -> project_remote_allowed_networks=[] (deny all)')
    if 'project_remote_allowed_networks' not in mcp_security:
        mcp_security['project_remote_allowed_networks'] = []
        migrated.append('v34 implicit project MCP deny-all -> project_remote_allowed_networks=[]')
removed = []
for key in keys_to_remove:
    if key in user:
        user.pop(key)
        removed.append(key)

for field_path in paths_to_remove:
    parts = field_path.split('.')
    parent = user
    ancestors = []
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            break
        ancestors.append((parent, part))
        parent = parent[part]
    else:
        key = parts[-1]
        if isinstance(parent, dict) and key in parent:
            parent.pop(key)
            removed.append(field_path)
            for container, child_key in reversed(ancestors):
                child = container.get(child_key)
                if isinstance(child, dict) and not child:
                    container.pop(child_key)
                else:
                    break

if migrated:
    print(f'Applied {len(migrated)} migration(s):')
    for m in migrated:
        print(f'  ~ {m}')
    print()

if removed:
    print(f'Removed {len(removed)} retired field(s):')
    for key in removed:
        print(f'  - {key}')
    print()

# ── Merge missing fields ─────────────────────────────────────────────────

added = []

def merge(target, source, path=''):
    \"\"\"Recursively merge source into target, adding missing keys only.\"\"\"
    for key, value in source.items():
        key_path = f'{path}.{key}' if path else key
        if key not in target:
            target[key] = copy.deepcopy(value)
            added.append(key_path)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            merge(target[key], value, key_path)

merge(user, example)

# Always update config_version
user['config_version'] = example_version

# ── Write ─────────────────────────────────────────────────────────────────

backup = config_path.with_suffix('.yaml.bak')
shutil.copy2(config_path, backup)
print(f'Backed up to {backup.name}')

with open(config_path, 'w', encoding='utf-8') as f:
    yaml.dump(user, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

if added:
    print(f'Added {len(added)} new field(s):')
    for a in added:
        print(f'  + {a}')

if not migrated and not removed and not added:
    print('No changes needed (version bumped only).')

print()
print(f'OK config.yaml upgraded to version {example_version}.')
print('  Please review the changes and set any new required values.')
"
