# Backend Contribution Guide

Read [AGENTS.md](AGENTS.md) before changing backend code.

## Workflow

```bash
uv sync
make format
make lint
POSTGRES_TEST_URL="postgresql+asyncpg://.../postgres" make test
```

Features and bug fixes use TDD. PostgreSQL integration tests create random disposable databases and must never point at a business database.

## Authority rules

- Authenticate first, then resolve immutable ProjectContext and capability.
- Private repositories always filter project + owner + resource key.
- Worker execution uses only Gateway-admitted Agent/Skill/MCP/Credential snapshots.
- No default-user, owner-only, global asset, filesystem Memory, or raw saver fallback.
- Secret-bearing input never enters declarative caches or logs.

## Configuration

Update the Pydantic model, `config.example.yaml`, focused tests, deployment values and current docs together. Removed keys remain only in the explicit app-config tombstone validator.

## Assets

New system assets are published through admin services; project assets are created through project services and immutable versions. Do not add a second config-file catalog.

## Verification

Run focused tests while developing, then backend format/lint and the complete core suite. The core suite includes real PostgreSQL tests and must use a disposable maintenance instance with zero skips.
