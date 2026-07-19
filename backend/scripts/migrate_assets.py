#!/usr/bin/env python3
# ruff: noqa: E402
"""显式迁移 legacy Agent、Skill 与 MCP 到 PostgreSQL asset catalog。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import mimetypes
import os
import stat
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.shared_assets.agent_service import AgentService
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_closure import McpCredentialClosureInvalid, McpCredentialClosureTarget, lock_mcp_credential_closures
from app.shared_assets.crypto import EncryptedEnvelope, decrypt_credential_payload, encrypt_credential_payload
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from app.shared_assets.mcp_service import McpCredentialSlot, McpDefinition, McpService
from app.shared_assets.models import AgentPayload, AssetScope, SkillArchiveFile
from app.shared_assets.skill_service import _analyze_skill_files
from deerflow.config.agents_config import AgentConfig
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    AssetCatalogStateRow,
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user.model import UserRow


class AssetMigrationError(RuntimeError):
    """不包含 source 内容或 secret 的迁移失败。"""


@dataclass(frozen=True)
class MigrationCursor:
    item_id: uuid.UUID


def _cursor(value: str) -> MigrationCursor:
    try:
        return MigrationCursor(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise argparse.ArgumentTypeError("resume cursor must be a UUID") from None


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("batch size must be a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("batch size must be a positive integer")
    return parsed


def build_migration_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="迁移 shared assets；默认 batch size 为 100")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-cursor", type=_cursor)
    parser.add_argument("--batch-size", type=_positive_int, default=100)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--owner-map", type=Path)
    parser.add_argument("--actor-user-id")
    return parser


@dataclass(frozen=True)
class SourceLayout:
    repo_root: Path
    data_root: Path


def resolve_data_root(repo_root: Path, explicit: Path | None) -> Path:
    """Mirror ``runtime_home`` while honoring the CLI's explicit project root."""

    if explicit is not None:
        return explicit.absolute()
    if configured := os.environ.get("DEER_FLOW_HOME"):
        return Path(configured).absolute()
    return (repo_root / ".deer-flow").absolute()


@dataclass(frozen=True)
class OwnerMap:
    default_projects: Mapping[str, uuid.UUID]
    legacy_shared_owner: str | None = None
    system_actor: str | None = None


@dataclass(frozen=True)
class InventoryFile:
    source_path: Path = field(repr=False)
    archive_path: str
    content: bytes = field(repr=False)
    sha256: str


def _skill_checksum(files: Sequence[InventoryFile]) -> str:
    canonical = json.dumps(
        [{"path": item.archive_path, "sha256": item.sha256, "size_bytes": len(item.content)} for item in files],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _read_regular_file(path: Path) -> bytes:
    """Read one source through pinned, no-follow directory descriptors."""

    parent_descriptor: int | None = None
    try:
        parent_descriptor = _open_directory_chain(path.parent)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AssetMigrationError("source is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(descriptor)
    except AssetMigrationError:
        raise
    except OSError:
        raise AssetMigrationError("source file cannot be read safely") from None
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _archive_root(paths: Sequence[Path]) -> Path:
    manifests = [path for path in paths if path.name == "SKILL.md"]
    if manifests:
        return manifests[0].parent
    return paths[0].parent


@dataclass(frozen=True)
class InventoryItem:
    item_id: uuid.UUID
    kind: str
    source_key: str
    source_label: str
    slug: str
    display_name: str
    scope: str
    project_id: uuid.UUID | None
    owner_user_id: str
    checksum: str
    files: tuple[InventoryFile, ...] = field(repr=False)
    payload: Mapping[str, object] = field(default_factory=dict, repr=False)
    status: str = "ready"

    @classmethod
    def for_skill(
        cls,
        *,
        source_key: str,
        slug: str,
        display_name: str,
        scope: str,
        project_id: uuid.UUID | None,
        owner_user_id: str,
        files: Sequence[Path],
        status: str = "ready",
        source_label: str = "skill",
    ) -> InventoryItem:
        ordered = tuple(sorted(files))
        if not ordered:
            raise AssetMigrationError("skill source is empty")
        root = _archive_root(ordered)
        snapshots: list[InventoryFile] = []
        for path in ordered:
            content = _read_regular_file(path)
            try:
                archive_path = path.relative_to(root).as_posix()
            except ValueError:
                archive_path = path.name
            snapshots.append(InventoryFile(path, archive_path, content, hashlib.sha256(content).hexdigest()))
        snapshot_tuple = tuple(snapshots)
        return cls(
            item_id=uuid.uuid5(uuid.NAMESPACE_URL, source_key),
            kind="skill",
            source_key=source_key,
            source_label=source_label,
            slug=slug,
            display_name=display_name,
            scope=scope,
            project_id=project_id,
            owner_user_id=owner_user_id,
            checksum=_skill_checksum(snapshot_tuple),
            files=snapshot_tuple,
            status=status,
        )


@dataclass(frozen=True)
class BackupResult:
    run_id: uuid.UUID
    run_dir: Path
    backup_dir: Path
    ledger_path: Path
    files: tuple[Path, ...]


def _open_directory_chain(
    path: Path,
    *,
    create: bool = False,
    exclusive_final: bool = False,
) -> int:
    """Return a pinned dirfd after ``openat(O_NOFOLLOW)`` on every component."""

    absolute = path.absolute()
    parts = absolute.parts
    if not parts or any(part in {"", ".", ".."} for part in parts[1:]):
        raise AssetMigrationError("path cannot be opened safely")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parts[0], flags)
    try:
        for index, component in enumerate(parts[1:], start=1):
            final = index == len(parts) - 1
            created = False
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    if final and exclusive_final:
                        raise AssetMigrationError("backup path already exists") from None
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_descriptor)
                raise AssetMigrationError("path component is not a directory")
            os.close(descriptor)
            descriptor = next_descriptor
            if created:
                os.fchmod(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _private_directory(path: Path, *, exclusive: bool = False) -> None:
    descriptor: int | None = None
    try:
        descriptor = _open_directory_chain(path, create=True, exclusive_final=exclusive)
        os.fchmod(descriptor, 0o700)
    except AssetMigrationError:
        raise
    except OSError:
        raise AssetMigrationError("backup path cannot be opened safely") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor: int | None = None
    try:
        parent_descriptor = _open_directory_chain(path.parent)
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise AssetMigrationError("secure backup write failed") from None
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def create_secure_backup(
    inventory: Sequence[InventoryItem],
    root: Path,
    *,
    keyring: CredentialKeyring | None = None,
    run_id: uuid.UUID | None = None,
) -> BackupResult:
    run_id = run_id or uuid.uuid4()
    _private_directory(root)
    run_dir = root / str(run_id)
    backup_dir = run_dir / "backup"
    _private_directory(run_dir, exclusive=True)
    _private_directory(backup_dir, exclusive=True)
    copied: list[Path] = []
    ledger: list[dict[str, object]] = []
    requires_encryption = any(item.kind == "mcp" and bool(item.payload.get("secret_payload")) for item in inventory)
    if requires_encryption and keyring is None:
        raise AssetMigrationError("active credential key is unavailable for secure backup")
    unique_sources: dict[tuple[Path, str, str], dict[str, object]] = {}
    for item in inventory:
        for source in item.files:
            source_identity = (source.source_path.absolute(), source.sha256, source.archive_path)
            existing = unique_sources.get(source_identity)
            if existing is not None:
                existing["item_ids"].append(str(item.item_id))  # type: ignore[union-attr]
                existing["source_key_hashes"].append(hashlib.sha256(item.source_key.encode()).hexdigest())  # type: ignore[union-attr]
                continue
            if source.source_path.is_symlink():
                raise AssetMigrationError("source symlink is not allowed")
            current = _read_regular_file(source.source_path)
            if hashlib.sha256(current).hexdigest() != source.sha256:
                raise AssetMigrationError("source checksum changed before backup")
            destination = backup_dir / f"{uuid.uuid4()}.dfab"
            encrypted = keyring is not None
            stored = (
                _encrypt_backup_content(
                    current,
                    keyring,
                    run_id=run_id,
                    checksum=source.sha256,
                    archive_path=source.archive_path,
                )
                if encrypted
                else current
            )
            _write_private_file(destination, stored)
            copied.append(destination)
            entry: dict[str, object] = {
                "item_ids": [str(item.item_id)],
                "source_key_hashes": [hashlib.sha256(item.source_key.encode()).hexdigest()],
                "archive_path": source.archive_path,
                "checksum": source.sha256,
                "size_bytes": len(current),
                "encrypted": encrypted,
                "backup_file": destination.name,
            }
            unique_sources[source_identity] = entry
            ledger.append(entry)
    ledger_path = run_dir / "ledger.json"
    _write_private_file(
        ledger_path,
        json.dumps(ledger, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )
    return BackupResult(run_id, run_dir, backup_dir, ledger_path, tuple(copied))


_BACKUP_MAGIC = b"DFAB1"


def _backup_aad(run_id: uuid.UUID, checksum: str, archive_path: str) -> bytes:
    return run_id.bytes + checksum.encode("ascii") + b"\0" + archive_path.encode("utf-8")


def _encrypt_backup_content(
    content: bytes,
    keyring: CredentialKeyring | None,
    *,
    run_id: uuid.UUID,
    checksum: str,
    archive_path: str,
) -> bytes:
    if keyring is None:
        raise AssetMigrationError("active credential key is unavailable for secure backup")
    key_id = keyring.active_key_id.encode("utf-8")
    if len(key_id) > 65535:
        raise AssetMigrationError("active credential key is unavailable for secure backup")
    nonce = os.urandom(12)
    ciphertext = AESGCM(keyring.key_for(keyring.active_key_id)).encrypt(
        nonce,
        content,
        _backup_aad(run_id, checksum, archive_path),
    )
    return _BACKUP_MAGIC + len(key_id).to_bytes(2, "big") + key_id + nonce + ciphertext


def restore_secure_backup(backup: BackupResult, keyring: CredentialKeyring) -> dict[str, bytes]:
    """Authenticate and reconstruct backup bytes without logging their content."""

    try:
        ledger = json.loads(_read_regular_file(backup.ledger_path).decode("utf-8"))
        if not isinstance(ledger, list):
            raise ValueError
        restored: dict[str, bytes] = {}
        for entry in ledger:
            if not isinstance(entry, dict):
                raise ValueError
            filename = str(entry["backup_file"])
            checksum = str(entry["checksum"])
            archive_path = str(entry["archive_path"])
            stored = _read_regular_file(backup.backup_dir / filename)
            if entry.get("encrypted"):
                if not stored.startswith(_BACKUP_MAGIC) or len(stored) < len(_BACKUP_MAGIC) + 2 + 12 + 16:
                    raise ValueError
                offset = len(_BACKUP_MAGIC)
                key_length = int.from_bytes(stored[offset : offset + 2], "big")
                offset += 2
                key_id = stored[offset : offset + key_length].decode("utf-8")
                offset += key_length
                nonce = stored[offset : offset + 12]
                ciphertext = stored[offset + 12 :]
                content = AESGCM(keyring.key_for(key_id)).decrypt(
                    nonce,
                    ciphertext,
                    _backup_aad(backup.run_id, checksum, archive_path),
                )
            else:
                content = stored
            if len(content) != int(entry["size_bytes"]) or hashlib.sha256(content).hexdigest() != checksum:
                raise ValueError
            restored[filename] = content
        return restored
    except (AssetMigrationError, CredentialKeyringInvalid):
        raise
    except Exception:
        raise AssetMigrationError("secure backup restore failed") from None


def _write_migration_status(backup: BackupResult, payload: Mapping[str, object]) -> None:
    _write_private_file(
        backup.run_dir / "result.json",
        json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )


def _stored_agent_payload(
    version: AgentVersionRow,
    skill_version_ids: Sequence[uuid.UUID],
    mcp_version_ids: Sequence[uuid.UUID],
) -> AgentPayload:
    return AgentPayload(
        description=str(version.description),
        soul=str(version.soul),
        model_ref=str(version.model_ref),
        tool_groups=tuple(version.tool_groups),
        skill_version_ids=tuple(uuid.UUID(str(value)) for value in skill_version_ids),
        mcp_version_ids=tuple(uuid.UUID(str(value)) for value in mcp_version_ids),
    )


def _stored_mcp_definition(
    version: McpServerVersionRow,
    slots: Sequence[McpCredentialSlotRow],
) -> McpDefinition:
    return McpDefinition(
        description=str(version.description),
        transport=str(version.transport),
        command=version.command,
        args=tuple(version.args),
        url=version.url,
        env=dict(version.non_secret_env),
        headers=dict(version.non_secret_headers),
        oauth=dict(version.oauth_metadata),
        routing=dict(version.routing),
        tool_overrides=dict(version.tool_overrides),
        timeout_seconds=int(version.timeout_seconds),
        credential_slots=tuple(
            McpCredentialSlot(
                name=str(slot.name),
                purpose=str(slot.purpose),
                payload_schema={key: tuple(sorted(values)) for key, values in dict(slot.payload_schema).items()},
                required=bool(slot.required),
            )
            for slot in slots
        ),
    )


def build_inventory(layout: SourceLayout, owners: OwnerMap) -> tuple[InventoryItem, ...]:
    items: list[InventoryItem] = []

    def add_skill(path: Path, source_key: str, scope: str, owner: str, project: uuid.UUID | None, status: str, label: str) -> None:
        entries = tuple(path.rglob("*"))
        if any(candidate.is_symlink() for candidate in entries):
            raise AssetMigrationError("skill source symlink is not allowed")
        files = tuple(candidate for candidate in entries if candidate.is_file())
        if files:
            item = InventoryItem.for_skill(
                source_key=source_key,
                slug=path.name,
                display_name=path.name,
                scope=scope,
                project_id=project,
                owner_user_id=owner,
                files=files,
                status=status,
                source_label=label,
            )
            items.append(item)

    def add_agent(path: Path, source_key: str, scope: str, owner: str, project: uuid.UUID | None, status: str, label: str) -> None:
        config_path = path / "config.yaml"
        soul_path = path / "SOUL.md"
        if not config_path.is_file() or not soul_path.is_file():
            return
        config_content = _read_regular_file(config_path)
        soul_content = _read_regular_file(soul_path)
        try:
            raw_config = yaml.safe_load(config_content.decode("utf-8")) or {}
            if not isinstance(raw_config, dict):
                raise ValueError
            config = AgentConfig.model_validate({**raw_config, "name": raw_config.get("name") or path.name})
        except Exception:
            raise AssetMigrationError("agent source validation failed") from None
        files = (
            InventoryFile(config_path, "config.yaml", config_content, hashlib.sha256(config_content).hexdigest()),
            InventoryFile(soul_path, "SOUL.md", soul_content, hashlib.sha256(soul_content).hexdigest()),
        )
        canonical = json.dumps(
            {"config_sha256": files[0].sha256, "soul_sha256": files[1].sha256},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        items.append(
            InventoryItem(
                item_id=uuid.uuid5(uuid.NAMESPACE_URL, source_key),
                kind="agent",
                source_key=source_key,
                source_label=label,
                slug=path.name,
                display_name=config.name,
                scope=scope,
                project_id=project,
                owner_user_id=owner,
                checksum=hashlib.sha256(canonical).hexdigest(),
                files=files,
                payload={
                    "description": config.description,
                    "model_ref": config.model or "default",
                    "tool_groups": config.tool_groups or [],
                    "skill_slugs": config.skills,
                    "mcp_slugs": raw_config.get("mcp_servers") if "mcp_servers" in raw_config else raw_config.get("mcp") if "mcp" in raw_config else None,
                    "soul": soul_content.decode("utf-8"),
                },
                status=status,
            )
        )

    def add_default_agent() -> None:
        """Snapshot the real no-``agent_name`` lead-agent configuration.

        Runtime selects the first configured model, all configured tool groups,
        all enabled skills/MCP servers, and the optional root ``SOUL.md``.
        """

        config_path = layout.repo_root / "config.yaml"
        if not config_path.is_file():
            return
        config_content = _read_regular_file(config_path)
        try:
            config = yaml.safe_load(config_content.decode("utf-8")) or {}
            models = config.get("models")
            groups = config.get("tool_groups") or []
            if not isinstance(config, dict) or not isinstance(models, list) or not models or not isinstance(models[0], dict) or not isinstance(models[0].get("name"), str):
                raise ValueError
            if not isinstance(groups, list) or any(not isinstance(group, dict) or not isinstance(group.get("name"), str) for group in groups):
                raise ValueError
        except (UnicodeError, yaml.YAMLError, TypeError, ValueError):
            raise AssetMigrationError("default agent source validation failed") from None
        soul_path = layout.data_root / "SOUL.md"
        soul_content = _read_regular_file(soul_path) if soul_path.is_file() else b""
        files = [InventoryFile(config_path, "config.yaml", config_content, hashlib.sha256(config_content).hexdigest())]
        if soul_path.is_file():
            files.append(InventoryFile(soul_path, "SOUL.md", soul_content, hashlib.sha256(soul_content).hexdigest()))
        canonical = json.dumps(
            {"files": [{"path": file.archive_path, "sha256": file.sha256} for file in files]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        items.append(
            InventoryItem(
                item_id=uuid.uuid5(uuid.NAMESPACE_URL, "system-agent:lead-agent"),
                kind="agent",
                source_key="system-agent:lead-agent",
                source_label="repo-default-agent",
                slug="lead-agent",
                display_name="lead-agent",
                scope="system",
                project_id=None,
                owner_user_id=owners.system_actor or "",
                checksum=hashlib.sha256(canonical).hexdigest(),
                files=tuple(files),
                payload={
                    "description": "",
                    "model_ref": models[0]["name"],
                    "tool_groups": tuple(group["name"] for group in groups),
                    "skill_slugs": None,
                    "mcp_slugs": None,
                    "soul": soul_content.decode("utf-8"),
                },
            )
        )

    public = layout.repo_root / "skills/public"
    if public.is_dir():
        for skill in sorted(public.iterdir()):
            if skill.is_dir():
                add_skill(skill, f"system-skill:{skill.name}", "system", owners.system_actor or "", None, "ready", "repo-public-skill")
    users = layout.data_root / "users"
    if users.is_dir():
        for user_dir in sorted(users.iterdir()):
            project = owners.default_projects.get(user_dir.name)
            status = "ready" if project is not None else "unresolved_owner"
            custom = user_dir / "skills/custom"
            if custom.is_dir():
                for skill in sorted(custom.iterdir()):
                    if skill.is_dir() and not skill.name.startswith("."):
                        add_skill(skill, f"user-skill:{user_dir.name}:{skill.name}", "project", user_dir.name, project, status, "user-custom-skill")
            agents = user_dir / "agents"
            if agents.is_dir():
                for agent in sorted(agents.iterdir()):
                    if agent.is_dir() and not agent.name.startswith("."):
                        add_agent(agent, f"user-agent:{user_dir.name}:{agent.name}", "project", user_dir.name, project, status, "user-custom-agent")
    legacy = layout.data_root / "skills/custom"
    if legacy.is_dir():
        legacy_owner = owners.legacy_shared_owner or ""
        project = owners.default_projects.get(legacy_owner)
        status = "ready" if legacy_owner and project is not None else "unresolved_owner"
        for skill in sorted(legacy.iterdir()):
            if skill.is_dir() and not skill.name.startswith("."):
                add_skill(skill, f"legacy-shared-skill:{skill.name}", "project", legacy_owner, project, status, "legacy-shared-skill")
    legacy_agents = layout.data_root / "agents"
    if legacy_agents.is_dir():
        legacy_owner = owners.legacy_shared_owner or ""
        project = owners.default_projects.get(legacy_owner)
        status = "ready" if legacy_owner and project is not None else "unresolved_owner"
        for agent in sorted(legacy_agents.iterdir()):
            if agent.is_dir() and not agent.name.startswith("."):
                add_agent(agent, f"legacy-shared-agent:{agent.name}", "project", legacy_owner, project, status, "legacy-shared-agent")
    repository_agents = layout.repo_root / "agents"
    if repository_agents.is_dir():
        for agent in sorted(repository_agents.iterdir()):
            if agent.is_dir() and not agent.name.startswith("."):
                add_agent(agent, f"system-agent:{agent.name}", "system", owners.system_actor or "", None, "ready", "repo-system-agent")
    add_default_agent()
    return tuple(sorted(items, key=lambda item: (item.kind, item.source_key)))


def render_inventory(inventory: Sequence[InventoryItem]) -> str:
    return "\n".join(
        json.dumps(
            {
                "source": item.source_label,
                "scope": item.scope,
                "target_project": str(item.project_id) if item.project_id else None,
                "kind": item.kind,
                "slug": item.slug,
                "checksum": item.checksum,
                "status": item.status,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for item in inventory
    )


def validate_executable_inventory(inventory: Sequence[InventoryItem]) -> None:
    if any(item.status != "ready" for item in inventory):
        raise AssetMigrationError("inventory contains unresolved_owner")


@dataclass(frozen=True)
class MigrationValidationProbes:
    counts: Callable[..., object]
    checksums: Callable[..., object]
    dependencies: Callable[..., object]
    decrypt: Callable[..., object]


@dataclass(frozen=True)
class MigrationResult:
    created_versions: int = 0
    noop_versions: int = 0
    resume_cursor: MigrationCursor | None = None
    run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class _DependencyCandidate:
    kind: str
    source_key: str | None
    slug: str
    scope: str
    project_id: uuid.UUID | None
    version_id: uuid.UUID
    active: bool


class AssetMigrationRunner:
    def __init__(
        self,
        session_factory,
        *,
        backup_root: Path,
        validation_probes: MigrationValidationProbes | None = None,
        keyring: CredentialKeyring | None = None,
        actor_user_id: str | None = None,
    ):
        self.session_factory = session_factory
        self.backup_root = backup_root
        self.validation_probes = validation_probes
        self.keyring = keyring
        self.actor_user_id = actor_user_id

    async def _validate_scope(self, session, item: InventoryItem) -> str:
        if item.scope == "system":
            actor_id = item.owner_user_id or self.actor_user_id
            if actor_id is None:
                candidates = tuple((await session.execute(select(UserRow.id).where(UserRow.system_role == "system_admin").order_by(UserRow.id).limit(2))).scalars())
                if len(candidates) != 1:
                    raise AssetMigrationError("system migration actor is unavailable")
                actor_id = str(candidates[0])
            actor = await session.get(UserRow, actor_id)
            if actor is None or actor.system_role != "system_admin":
                raise AssetMigrationError("system migration actor is unavailable")
            return actor_id
        if item.scope != "project" or item.project_id is None or not item.owner_user_id:
            raise AssetMigrationError("project source has unresolved_owner")
        project = (
            await session.execute(
                select(ProjectRow.id)
                .join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id)
                .where(
                    ProjectRow.id == item.project_id,
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                    ProjectMembershipRow.user_id == item.owner_user_id,
                    ProjectMembershipRow.status == "active",
                )
            )
        ).scalar_one_or_none()
        if project is None:
            raise AssetMigrationError("mapped default project is unavailable")
        return item.owner_user_id

    async def _migrate_skill(self, session, item: InventoryItem) -> tuple[int, int]:
        asset = (await session.execute(select(SkillRow).where(SkillRow.source_key == item.source_key).with_for_update(of=SkillRow))).scalar_one_or_none()
        if asset is not None:
            if asset.scope != item.scope or asset.project_id != item.project_id or asset.slug.casefold() != item.slug.casefold():
                raise AssetMigrationError("source_key conflicts with an existing asset")
            imported = (
                await session.execute(
                    select(SkillVersionRow.id).where(
                        SkillVersionRow.skill_id == asset.id,
                        SkillVersionRow.payload_checksum == item.checksum,
                        SkillVersionRow.review_note == f"migration-source:{item.checksum}",
                    )
                )
            ).scalar_one_or_none()
            if imported is not None:
                imported_row = await session.get(SkillVersionRow, imported)
                if imported_row is None or imported_row.workflow_status not in {"draft", "published"}:
                    raise AssetMigrationError("imported skill version cannot be published")
                if imported_row.workflow_status == "draft":
                    imported_row.workflow_status = "published"
                if asset.current_published_version_id != imported:
                    asset.current_published_version_id = imported
                    asset.version += 1
                    await session.flush()
                return 0, 1
        archive = tuple(
            SkillArchiveFile(
                path=file.archive_path,
                content=file.content,
                media_type=mimetypes.guess_type(file.archive_path)[0] or "application/octet-stream",
            )
            for file in item.files
        )
        try:
            preview = await asyncio.to_thread(_analyze_skill_files, archive, "asset-migration")
        except Exception:
            raise AssetMigrationError("skill source validation failed") from None
        if preview.checksum != item.checksum:
            raise AssetMigrationError("skill checksum validation failed")
        if asset is None:
            asset = SkillRow(
                scope=item.scope,
                project_id=item.project_id,
                slug=item.slug,
                display_name=item.display_name,
                source_key=item.source_key,
                created_by_user_id=item.owner_user_id,
            )
            session.add(asset)
            await session.flush()
            version_number = 1
            supersedes = None
        else:
            version_number = int((await session.execute(select(func.coalesce(func.max(SkillVersionRow.version_number), 0) + 1).where(SkillVersionRow.skill_id == asset.id))).scalar_one())
            supersedes = asset.current_published_version_id
        version = SkillVersionRow(
            id=self._planned_version_id(item),
            skill_id=asset.id,
            version_number=version_number,
            workflow_status="draft",
            description=preview.description,
            frontmatter=dict(preview.frontmatter),
            compatibility=preview.compatibility,
            secret_requirements=[{"name": requirement.name, "optional": requirement.optional} for requirement in preview.secret_requirements],
            scan_decision=preview.scan_decision,
            scan_summary=dict(preview.scan_summary),
            supersedes_version_id=supersedes,
            payload_checksum=preview.checksum,
            review_note=f"migration-source:{item.checksum}",
            created_by_user_id=item.owner_user_id,
        )
        session.add(version)
        await session.flush()
        for file, view in zip(preview.files, preview.file_views, strict=True):
            session.add(
                SkillVersionFileRow(
                    skill_version_id=version.id,
                    path=file.path,
                    media_type=file.media_type,
                    size_bytes=view.size_bytes,
                    sha256=view.sha256,
                    content=file.content,
                )
            )
        await session.flush()
        version.workflow_status = "published"
        asset.current_published_version_id = version.id
        if version_number > 1:
            asset.version += 1
        await session.flush()
        return 1, 0

    @staticmethod
    def _planned_version_id(item: InventoryItem) -> uuid.UUID:
        try:
            return uuid.UUID(str(item.payload["_planned_version_id"]))
        except (KeyError, TypeError, ValueError):
            raise AssetMigrationError("migration dependency version was not frozen") from None

    @staticmethod
    def _agent_payload(item: InventoryItem) -> AgentPayload:
        try:
            skill_ids = tuple(uuid.UUID(str(value)) for value in item.payload["_frozen_skill_version_ids"])
            mcp_ids = tuple(uuid.UUID(str(value)) for value in item.payload["_frozen_mcp_version_ids"])
            return AgentPayload(
                description=str(item.payload.get("description") or ""),
                soul=str(item.payload["soul"]),
                model_ref=str(item.payload.get("model_ref") or "default"),
                tool_groups=tuple(item.payload.get("tool_groups") or ()),
                skill_version_ids=skill_ids,
                mcp_version_ids=mcp_ids,
            )
        except (KeyError, TypeError, ValueError):
            raise AssetMigrationError("agent source validation failed") from None

    async def _freeze_planned_dependency(
        self,
        session,
        item: InventoryItem,
    ) -> tuple[InventoryItem, _DependencyCandidate]:
        if item.kind == "skill":
            asset_model, version_model, parent_column = SkillRow, SkillVersionRow, SkillVersionRow.skill_id
        else:
            asset_model, version_model, parent_column = McpServerRow, McpServerVersionRow, McpServerVersionRow.mcp_server_id
        asset = (await session.execute(select(asset_model).where(asset_model.source_key == item.source_key))).scalar_one_or_none()
        if asset is not None and (asset.scope != item.scope or asset.project_id != item.project_id or asset.slug.casefold() != item.slug.casefold()):
            raise AssetMigrationError("source_key conflicts with an existing asset")
        imported_rows = ()
        if asset is not None:
            imported_rows = tuple(
                (
                    await session.execute(
                        select(version_model).where(
                            parent_column == asset.id,
                            version_model.payload_checksum == item.checksum,
                            version_model.review_note == f"migration-source:{item.checksum}",
                        )
                    )
                ).scalars()
            )
        if len(imported_rows) > 1:
            raise AssetMigrationError("imported dependency version is ambiguous")
        imported = imported_rows[0] if imported_rows else None
        if imported is not None and imported.workflow_status not in {"draft", "published"}:
            raise AssetMigrationError("imported dependency version cannot be published")
        version_id = uuid.UUID(str(imported.id)) if imported is not None else uuid.uuid4()
        payload = dict(item.payload)
        payload["_planned_version_id"] = version_id
        frozen = replace(item, payload=payload)
        active = (asset is None or asset.status == "active") if item.kind == "skill" else str(item.payload.get("asset_status") or "active") == "active"
        return frozen, _DependencyCandidate(
            kind=item.kind,
            source_key=item.source_key,
            slug=item.slug,
            scope=item.scope,
            project_id=item.project_id,
            version_id=version_id,
            active=active,
        )

    async def _existing_dependency_candidates(
        self,
        session,
        planned_source_keys: Mapping[str, set[str]],
    ) -> tuple[_DependencyCandidate, ...]:
        candidates: list[_DependencyCandidate] = []
        for kind, asset_model, version_model in (
            ("skill", SkillRow, SkillVersionRow),
            ("mcp", McpServerRow, McpServerVersionRow),
        ):
            rows = (
                await session.execute(
                    select(
                        asset_model.source_key,
                        asset_model.slug,
                        asset_model.scope,
                        asset_model.project_id,
                        version_model.id,
                    )
                    .join(version_model, version_model.id == asset_model.current_published_version_id)
                    .where(
                        asset_model.status == "active",
                        version_model.workflow_status == "published",
                    )
                )
            ).all()
            candidates.extend(
                _DependencyCandidate(
                    kind=kind,
                    source_key=row.source_key,
                    slug=row.slug,
                    scope=row.scope,
                    project_id=row.project_id,
                    version_id=uuid.UUID(str(row.id)),
                    active=True,
                )
                for row in rows
                if row.source_key not in planned_source_keys[kind]
            )
        return tuple(candidates)

    @staticmethod
    def _freeze_agent_dependencies(
        item: InventoryItem,
        kind: str,
        candidates: Sequence[_DependencyCandidate],
    ) -> tuple[uuid.UUID, ...]:
        visible = tuple(
            candidate for candidate in candidates if candidate.kind == kind and candidate.active and (candidate.scope == "system" or (item.scope == "project" and candidate.scope == "project" and candidate.project_id == item.project_id))
        )
        configured = item.payload.get(f"{kind}_slugs")
        if configured is None:
            selected = sorted(visible, key=lambda candidate: (candidate.scope, candidate.slug.casefold(), candidate.source_key or ""))
            slugs = [candidate.slug.casefold() for candidate in selected]
            if len(slugs) != len(set(slugs)):
                raise AssetMigrationError("agent dependency is missing or ambiguous")
            return tuple(candidate.version_id for candidate in selected)
        if not isinstance(configured, (list, tuple)) or any(not isinstance(slug, str) for slug in configured):
            raise AssetMigrationError("agent source validation failed")
        resolved: list[uuid.UUID] = []
        for slug in configured:
            matches = [candidate for candidate in visible if candidate.slug.casefold() == slug.casefold()]
            if len(matches) != 1:
                raise AssetMigrationError("agent dependency is missing or ambiguous")
            resolved.append(matches[0].version_id)
        if len(resolved) != len(set(resolved)):
            raise AssetMigrationError("agent dependency is missing or ambiguous")
        return tuple(resolved)

    async def _preflight_inventory(self, inventory: Sequence[InventoryItem]) -> tuple[InventoryItem, ...]:
        """Freeze scopes and the complete dependency graph before the first write."""

        effective: list[InventoryItem] = []
        async with self.session_factory() as session:
            async with session.begin():
                for item in inventory:
                    actor_id = await self._validate_scope(session, item)
                    effective.append(item if item.owner_user_id == actor_id else replace(item, owner_user_id=actor_id))

                frozen: list[InventoryItem] = []
                planned_candidates: list[_DependencyCandidate] = []
                planned_source_keys = {"skill": set(), "mcp": set()}
                for item in effective:
                    if item.kind in planned_source_keys:
                        frozen_item, candidate = await self._freeze_planned_dependency(session, item)
                        frozen.append(frozen_item)
                        planned_candidates.append(candidate)
                        planned_source_keys[item.kind].add(item.source_key)
                    else:
                        frozen.append(item)
                existing_candidates = await self._existing_dependency_candidates(session, planned_source_keys)
                candidates = (*existing_candidates, *planned_candidates)
                result: list[InventoryItem] = []
                for item in frozen:
                    if item.kind != "agent":
                        result.append(item)
                        continue
                    payload = dict(item.payload)
                    payload["_frozen_skill_version_ids"] = self._freeze_agent_dependencies(item, "skill", candidates)
                    payload["_frozen_mcp_version_ids"] = self._freeze_agent_dependencies(item, "mcp", candidates)
                    result.append(replace(item, payload=payload))
        return tuple(result)

    async def _migrate_agent(self, session, item: InventoryItem) -> tuple[int, int]:
        asset = (await session.execute(select(AgentRow).where(AgentRow.source_key == item.source_key).with_for_update(of=AgentRow))).scalar_one_or_none()
        if asset is not None:
            if asset.scope != item.scope or asset.project_id != item.project_id or asset.slug.casefold() != item.slug.casefold():
                raise AssetMigrationError("source_key conflicts with an existing asset")
            imported = (
                await session.execute(
                    select(AgentVersionRow).where(
                        AgentVersionRow.agent_id == asset.id,
                        AgentVersionRow.review_note == f"migration-source:{item.checksum}",
                    )
                )
            ).scalar_one_or_none()
            if imported is not None:
                if imported.workflow_status not in {"draft", "published"}:
                    raise AssetMigrationError("imported agent version cannot be published")
                imported.workflow_status = "published"
                if asset.current_published_version_id != imported.id:
                    asset.current_published_version_id = imported.id
                    asset.version += 1
                await session.flush()
                return 0, 1
        payload = self._agent_payload(item)
        checksum = AgentService._payload_checksum(payload)
        if asset is None:
            asset = AgentRow(
                scope=item.scope,
                project_id=item.project_id,
                slug=item.slug,
                display_name=item.display_name,
                source_key=item.source_key,
                created_by_user_id=item.owner_user_id,
            )
            session.add(asset)
            await session.flush()
            number, supersedes = 1, None
        else:
            number = int((await session.execute(select(func.coalesce(func.max(AgentVersionRow.version_number), 0) + 1).where(AgentVersionRow.agent_id == asset.id))).scalar_one())
            supersedes = asset.current_published_version_id
        version = AgentVersionRow(
            agent_id=asset.id,
            version_number=number,
            workflow_status="draft",
            description=payload.description,
            soul=payload.soul,
            model_ref=payload.model_ref,
            tool_groups=list(payload.tool_groups),
            supersedes_version_id=supersedes,
            payload_checksum=checksum,
            review_note=f"migration-source:{item.checksum}",
            created_by_user_id=item.owner_user_id,
        )
        session.add(version)
        await session.flush()
        session.add_all(
            [AgentVersionSkillRefRow(agent_version_id=version.id, skill_version_id=value, sort_order=index) for index, value in enumerate(payload.skill_version_ids)]
            + [AgentVersionMcpRefRow(agent_version_id=version.id, mcp_server_version_id=value, sort_order=index) for index, value in enumerate(payload.mcp_version_ids)]
        )
        await session.flush()
        version.workflow_status = "published"
        asset.current_published_version_id = version.id
        if number > 1:
            asset.version += 1
        await session.flush()
        return 1, 0

    @staticmethod
    def _resolve_environment_secrets(payload: Mapping[str, object]) -> dict[str, object]:
        resolved: dict[str, object] = {}
        for section, values in payload.items():
            if not isinstance(values, Mapping):
                raise AssetMigrationError("MCP credential payload invalid")
            section_values: dict[str, object] = {}
            for key, value in values.items():
                if isinstance(value, str) and value.startswith("$") and len(value) > 1:
                    environment_value = os.environ.get(value[1:])
                    if environment_value is None:
                        raise AssetMigrationError("MCP credential environment reference is unavailable")
                    value = environment_value
                section_values[str(key)] = value
            resolved[section] = section_values
        return resolved

    async def _ensure_mcp_credential(self, session, item: InventoryItem, payload: Mapping[str, object]) -> tuple[int, uuid.UUID | None]:
        if not payload:
            return 0, None
        if self.keyring is None:
            raise AssetMigrationError("active credential key is unavailable")
        resolved_payload = self._resolve_environment_secrets(payload)
        source_key = f"{item.source_key}:credential"
        credential = (await session.execute(select(CredentialRow).where(CredentialRow.source_key == source_key).with_for_update(of=CredentialRow))).scalar_one_or_none()
        if credential is not None:
            if credential.scope != item.scope or credential.project_id != item.project_id or credential.current_version_id is None:
                raise AssetMigrationError("MCP credential source conflicts")
            current = await session.get(CredentialVersionRow, credential.current_version_id, with_for_update=True)
            if current is None:
                raise AssetMigrationError("MCP credential envelope is unavailable")
            envelope = (
                await session.execute(
                    select(CredentialEnvelopeRow)
                    .where(
                        CredentialEnvelopeRow.credential_version_id == current.id,
                        CredentialEnvelopeRow.is_active.is_(True),
                    )
                    .with_for_update(of=CredentialEnvelopeRow)
                )
            ).scalar_one_or_none()
            if envelope is None:
                raise AssetMigrationError("MCP credential envelope is unavailable")
            try:
                existing_payload = decrypt_credential_payload(
                    EncryptedEnvelope(envelope.key_id, bytes(envelope.nonce), bytes(envelope.ciphertext)),
                    credential.scope,
                    credential.project_id,
                    uuid.UUID(str(current.id)),
                    self.keyring,
                )
            except Exception:
                raise AssetMigrationError("MCP credential decrypt probe failed") from None
            if existing_payload == resolved_payload:
                return 0, uuid.UUID(str(current.id))
            current.status = "retired"
            current.retired_at = datetime.now(UTC)
            number = int((await session.execute(select(func.coalesce(func.max(CredentialVersionRow.version_number), 0) + 1).where(CredentialVersionRow.credential_id == credential.id))).scalar_one())
            supersedes = current.id
        else:
            credential = CredentialRow(
                scope=item.scope,
                project_id=item.project_id,
                name=f"{item.slug[:50]}-legacy",
                display_name=f"{item.display_name[:100]} credential",
                credential_type="mcp",
                source_key=source_key,
                created_by_user_id=item.owner_user_id,
            )
            session.add(credential)
            await session.flush()
            number, supersedes = 1, None
        version_id = uuid.uuid4()
        schema = {section: sorted(values) for section, values in resolved_payload.items()}
        encrypted = encrypt_credential_payload(
            resolved_payload,
            item.scope,
            item.project_id,
            version_id,
            self.keyring,
        )
        version = CredentialVersionRow(
            id=version_id,
            credential_id=credential.id,
            version_number=number,
            status="active",
            payload_schema_version=1,
            payload_schema=schema,
            supersedes_version_id=supersedes,
            created_by_user_id=item.owner_user_id,
        )
        session.add(version)
        await session.flush()
        session.add(
            CredentialEnvelopeRow(
                credential_version_id=version.id,
                envelope_generation=1,
                key_id=encrypted.key_id,
                nonce=encrypted.nonce,
                ciphertext=encrypted.ciphertext,
                is_active=True,
                created_by_user_id=item.owner_user_id,
                activated_at=datetime.now(UTC),
            )
        )
        credential.current_version_id = version.id
        if number > 1:
            credential.version += 1
        await session.flush()
        return 1, uuid.UUID(str(version.id))

    async def _ensure_mcp_grant(
        self,
        session,
        item: InventoryItem,
        *,
        mcp_version_id: uuid.UUID,
        credential_version_id: uuid.UUID | None,
    ) -> None:
        slot = (
            await session.execute(
                select(McpCredentialSlotRow).where(
                    McpCredentialSlotRow.mcp_server_version_id == mcp_version_id,
                    McpCredentialSlotRow.name == "legacy-secrets",
                    McpCredentialSlotRow.required.is_(True),
                )
            )
        ).scalar_one_or_none()
        if credential_version_id is None:
            if slot is not None:
                raise AssetMigrationError("MCP credential closure is incomplete")
            return
        if slot is None:
            raise AssetMigrationError("MCP credential closure is incomplete")
        existing = (
            await session.execute(
                select(CredentialGrantRow)
                .where(
                    CredentialGrantRow.mcp_server_version_id == mcp_version_id,
                    CredentialGrantRow.credential_slot_id == slot.id,
                    CredentialGrantRow.status == "active",
                )
                .with_for_update(of=CredentialGrantRow)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.credential_version_id == credential_version_id:
            return
        if existing is not None:
            existing.status = "revoked"
            existing.revoked_at = datetime.now(UTC)
            existing.revoked_by_user_id = item.owner_user_id
            existing.version += 1
            await session.flush()
        session.add(
            CredentialGrantRow(
                mcp_server_version_id=mcp_version_id,
                credential_slot_id=slot.id,
                credential_version_id=credential_version_id,
                created_by_user_id=item.owner_user_id,
            )
        )
        await session.flush()

    async def _migrate_mcp(self, session, item: InventoryItem) -> tuple[int, int]:
        definition = item.payload.get("definition")
        secret_payload = item.payload.get("secret_payload") or {}
        if not isinstance(definition, McpDefinition) or not isinstance(secret_payload, Mapping):
            raise AssetMigrationError("MCP source validation failed")
        try:
            definition = McpService._validate_definition(
                SystemAssetGovernanceContext(
                    user_id=uuid.UUID(item.owner_user_id),
                    request_id="asset-migration",
                    project_id=item.project_id,
                ),
                definition,
            )
        except Exception:
            raise AssetMigrationError("MCP source validation failed") from None
        checksum = McpService._checksum(definition)
        if checksum != item.checksum:
            raise AssetMigrationError("MCP checksum validation failed")
        asset = (await session.execute(select(McpServerRow).where(McpServerRow.source_key == item.source_key).with_for_update(of=McpServerRow))).scalar_one_or_none()
        if asset is not None:
            if asset.scope != item.scope or asset.project_id != item.project_id or asset.slug.casefold() != item.slug.casefold():
                raise AssetMigrationError("source_key conflicts with an existing asset")
            desired_status = str(item.payload.get("asset_status") or "active")
            if asset.status != desired_status:
                asset.status = desired_status
                asset.version += 1
            imported = (
                await session.execute(
                    select(McpServerVersionRow).where(
                        McpServerVersionRow.mcp_server_id == asset.id,
                        McpServerVersionRow.payload_checksum == checksum,
                        McpServerVersionRow.review_note == f"migration-source:{item.checksum}",
                    )
                )
            ).scalar_one_or_none()
            if imported is not None:
                if imported.workflow_status not in {"draft", "published"}:
                    raise AssetMigrationError("imported MCP version cannot be published")
                imported.workflow_status = "published"
                if asset.current_published_version_id != imported.id:
                    asset.current_published_version_id = imported.id
                    asset.version += 1
                credential_versions, credential_version_id = await self._ensure_mcp_credential(session, item, secret_payload)
                await self._ensure_mcp_grant(
                    session,
                    item,
                    mcp_version_id=uuid.UUID(str(imported.id)),
                    credential_version_id=credential_version_id,
                )
                await session.flush()
                return credential_versions, 1
        if asset is None:
            asset = McpServerRow(
                scope=item.scope,
                project_id=item.project_id,
                slug=item.slug,
                display_name=item.display_name,
                source_key=item.source_key,
                created_by_user_id=item.owner_user_id,
                status=str(item.payload.get("asset_status") or "active"),
            )
            session.add(asset)
            await session.flush()
            number, supersedes = 1, None
        else:
            number = int((await session.execute(select(func.coalesce(func.max(McpServerVersionRow.version_number), 0) + 1).where(McpServerVersionRow.mcp_server_id == asset.id))).scalar_one())
            supersedes = asset.current_published_version_id
        version = McpServerVersionRow(
            id=self._planned_version_id(item),
            mcp_server_id=asset.id,
            version_number=number,
            workflow_status="draft",
            description=definition.description,
            transport=definition.transport,
            command=definition.command,
            args=list(definition.args),
            url=definition.url,
            non_secret_env=dict(definition.env),
            non_secret_headers=dict(definition.headers),
            oauth_metadata=dict(definition.oauth),
            routing=dict(definition.routing),
            tool_overrides=dict(definition.tool_overrides),
            timeout_seconds=definition.timeout_seconds,
            supersedes_version_id=supersedes,
            payload_checksum=checksum,
            review_note=f"migration-source:{item.checksum}",
            created_by_user_id=item.owner_user_id,
        )
        session.add(version)
        await session.flush()
        session.add_all(
            [
                McpCredentialSlotRow(
                    mcp_server_version_id=version.id,
                    name=slot.name,
                    purpose=slot.purpose,
                    payload_schema={key: list(values) for key, values in slot.payload_schema.items()},
                    required=slot.required,
                )
                for slot in definition.credential_slots
            ]
        )
        await session.flush()
        credential_versions, credential_version_id = await self._ensure_mcp_credential(session, item, secret_payload)
        await self._ensure_mcp_grant(
            session,
            item,
            mcp_version_id=uuid.UUID(str(version.id)),
            credential_version_id=credential_version_id,
        )
        version.workflow_status = "published"
        asset.current_published_version_id = version.id
        if number > 1:
            asset.version += 1
        await session.flush()
        return 1 + credential_versions, 0

    async def _current_imported_version(self, session, item: InventoryItem):
        types = {
            "agent": (AgentRow, AgentVersionRow, AgentVersionRow.agent_id),
            "skill": (SkillRow, SkillVersionRow, SkillVersionRow.skill_id),
            "mcp": (McpServerRow, McpServerVersionRow, McpServerVersionRow.mcp_server_id),
        }
        selected = types.get(item.kind)
        if selected is None:
            return None
        asset_model, version_model, parent_column = selected
        return (
            await session.execute(
                select(version_model)
                .join(asset_model, asset_model.id == parent_column)
                .where(
                    asset_model.source_key == item.source_key,
                    asset_model.current_published_version_id == version_model.id,
                    version_model.workflow_status == "published",
                    version_model.review_note == f"migration-source:{item.checksum}",
                )
            )
        ).scalar_one_or_none()

    async def _default_counts_probe(self, session, inventory: Sequence[InventoryItem]) -> bool:
        return all([await self._current_imported_version(session, item) is not None for item in inventory])

    async def _default_checksums_probe(self, session, inventory: Sequence[InventoryItem]) -> bool:
        for item in inventory:
            if item.kind == "skill":
                version = await self._current_imported_version(session, item)
                if version is None:
                    return False
                files = tuple((await session.execute(select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == version.id).order_by(SkillVersionFileRow.path))).scalars().all())
                canonical = json.dumps(
                    [{"path": file.path, "sha256": file.sha256, "size_bytes": file.size_bytes} for file in files],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                if hashlib.sha256(canonical).hexdigest() != item.checksum:
                    return False
                if any(hashlib.sha256(file.content).hexdigest() != file.sha256 for file in files):
                    return False
                try:
                    archive = tuple(
                        SkillArchiveFile(
                            path=file.path,
                            content=bytes(file.content),
                            media_type=file.media_type,
                        )
                        for file in files
                    )
                    preview = await asyncio.to_thread(_analyze_skill_files, archive, "asset-migration-probe")
                except Exception:
                    return False
                if (
                    version.payload_checksum != preview.checksum
                    or version.description != preview.description
                    or dict(version.frontmatter) != dict(preview.frontmatter)
                    or version.compatibility != preview.compatibility
                    or list(version.secret_requirements) != [{"name": requirement.name, "optional": requirement.optional} for requirement in preview.secret_requirements]
                    or version.scan_decision != preview.scan_decision
                    or dict(version.scan_summary) != dict(preview.scan_summary)
                ):
                    return False
            elif item.kind == "mcp":
                version = await self._current_imported_version(session, item)
                if version is None:
                    return False
                slots = tuple((await session.execute(select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == version.id).order_by(McpCredentialSlotRow.name, McpCredentialSlotRow.id))).scalars().all())
                definition = _stored_mcp_definition(version, slots)
                if version.payload_checksum != item.checksum or McpService._checksum(definition) != version.payload_checksum:
                    return False
            elif item.kind == "agent":
                version = await self._current_imported_version(session, item)
                if version is None:
                    return False
                skill_ids = tuple((await session.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == version.id).order_by(AgentVersionSkillRefRow.sort_order))).scalars())
                mcp_ids = tuple((await session.execute(select(AgentVersionMcpRefRow.mcp_server_version_id).where(AgentVersionMcpRefRow.agent_version_id == version.id).order_by(AgentVersionMcpRefRow.sort_order))).scalars())
                payload = _stored_agent_payload(version, skill_ids, mcp_ids)
                if AgentService._payload_checksum(payload) != version.payload_checksum:
                    return False
            else:
                return False
        return True

    async def _default_dependencies_probe(self, session, inventory: Sequence[InventoryItem]) -> bool:
        for item in inventory:
            if item.kind == "agent":
                version = await self._current_imported_version(session, item)
                if version is None:
                    return False
                skill_ids = tuple((await session.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == version.id).order_by(AgentVersionSkillRefRow.sort_order))).scalars())
                mcp_ids = tuple((await session.execute(select(AgentVersionMcpRefRow.mcp_server_version_id).where(AgentVersionMcpRefRow.agent_version_id == version.id).order_by(AgentVersionMcpRefRow.sort_order))).scalars())
                payload = _stored_agent_payload(version, skill_ids, mcp_ids)
                if AgentService._payload_checksum(payload) != version.payload_checksum:
                    return False
                for dependency_kind, dependency_ids in (("skill", payload.skill_version_ids), ("mcp", payload.mcp_version_ids)):
                    asset_model = SkillRow if dependency_kind == "skill" else McpServerRow
                    version_model = SkillVersionRow if dependency_kind == "skill" else McpServerVersionRow
                    parent_column = SkillVersionRow.skill_id if dependency_kind == "skill" else McpServerVersionRow.mcp_server_id
                    allowed = asset_model.scope == "system"
                    if item.scope == "project":
                        allowed = allowed | ((asset_model.scope == "project") & (asset_model.project_id == item.project_id))
                    for dependency_id in dependency_ids:
                        exists = (
                            await session.execute(
                                select(version_model.id)
                                .join(asset_model, asset_model.id == parent_column)
                                .where(
                                    version_model.id == dependency_id,
                                    version_model.workflow_status == "published",
                                    asset_model.status == "active",
                                    allowed,
                                )
                            )
                        ).scalar_one_or_none()
                        if exists is None:
                            return False
            elif item.kind == "mcp":
                mcp = (await session.execute(select(McpServerRow).where(McpServerRow.source_key == item.source_key))).scalar_one_or_none()
                if mcp is None or mcp.current_published_version_id is None:
                    return False
                try:
                    await lock_mcp_credential_closures(
                        session,
                        (
                            McpCredentialClosureTarget(
                                uuid.UUID(str(mcp.current_published_version_id)),
                                AssetScope(item.scope),
                                item.project_id,
                            ),
                        ),
                    )
                except McpCredentialClosureInvalid:
                    return False
        return True

    async def _default_decrypt_probe(self, session, inventory: Sequence[InventoryItem]) -> bool:
        for item in inventory:
            if item.kind != "mcp":
                continue
            raw_payload = item.payload.get("secret_payload") or {}
            if not raw_payload:
                continue
            if self.keyring is None or not isinstance(raw_payload, Mapping):
                return False
            expected = self._resolve_environment_secrets(raw_payload)
            credential = (await session.execute(select(CredentialRow).where(CredentialRow.source_key == f"{item.source_key}:credential"))).scalar_one_or_none()
            if credential is None or credential.current_version_id is None:
                return False
            version = await session.get(CredentialVersionRow, credential.current_version_id)
            envelope = (
                await session.execute(
                    select(CredentialEnvelopeRow).where(
                        CredentialEnvelopeRow.credential_version_id == credential.current_version_id,
                        CredentialEnvelopeRow.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if version is None or envelope is None:
                return False
            mcp = (await session.execute(select(McpServerRow).where(McpServerRow.source_key == item.source_key))).scalar_one_or_none()
            if mcp is None or mcp.current_published_version_id is None:
                return False
            try:
                closure = (
                    await lock_mcp_credential_closures(
                        session,
                        (
                            McpCredentialClosureTarget(
                                uuid.UUID(str(mcp.current_published_version_id)),
                                AssetScope(item.scope),
                                item.project_id,
                            ),
                        ),
                        load_envelopes=True,
                    )
                )[uuid.UUID(str(mcp.current_published_version_id))]
            except McpCredentialClosureInvalid:
                return False
            if len(closure.materials) != 1 or closure.materials[0].version.id != version.id:
                return False
            try:
                decrypted = decrypt_credential_payload(
                    EncryptedEnvelope(envelope.key_id, bytes(envelope.nonce), bytes(envelope.ciphertext)),
                    credential.scope,
                    credential.project_id,
                    uuid.UUID(str(version.id)),
                    self.keyring,
                )
            except Exception:
                return False
            if decrypted != expected:
                return False
        return True

    async def _call_probe(self, name: str, callback, session, inventory: Sequence[InventoryItem]) -> None:
        result = callback(session, inventory)
        if inspect.isawaitable(result):
            result = await result
        if result is not True:
            raise AssetMigrationError(f"{name} validation failed")

    async def _validate_catalog(self, inventory: Sequence[InventoryItem]) -> None:
        defaults = MigrationValidationProbes(
            counts=self._default_counts_probe,
            checksums=self._default_checksums_probe,
            dependencies=self._default_dependencies_probe,
            decrypt=self._default_decrypt_probe,
        )
        probes = self.validation_probes or defaults
        async with self.session_factory() as session:
            async with session.begin():
                state = (await session.execute(select(AssetCatalogStateRow).where(AssetCatalogStateRow.id == 1).with_for_update())).scalar_one_or_none()
                if state is None:
                    state = AssetCatalogStateRow(id=1, generation=1)
                    session.add(state)
                    await session.flush()
                for name in ("counts", "checksums", "dependencies", "decrypt"):
                    await self._call_probe(name, getattr(probes, name), session, inventory)
                state.updated_at = datetime.now(UTC)

    async def run(
        self,
        inventory: Sequence[InventoryItem],
        *,
        execute: bool,
        resume_cursor: MigrationCursor | None = None,
        batch_size: int = 100,
    ) -> MigrationResult:
        kind_order = {"skill": 0, "mcp": 1, "agent": 2}
        full_snapshot = tuple(sorted(inventory, key=lambda item: (kind_order.get(item.kind, 99), item.source_key, item.item_id)))
        validate_executable_inventory(full_snapshot)
        snapshot = full_snapshot
        if batch_size < 1:
            raise AssetMigrationError("batch size must be positive")
        if resume_cursor is not None:
            positions = [index for index, item in enumerate(snapshot) if item.item_id == resume_cursor.item_id]
            if len(positions) != 1:
                raise AssetMigrationError("resume cursor does not match inventory")
            snapshot = snapshot[positions[0] + 1 :]
        if not execute:
            return MigrationResult(resume_cursor=MigrationCursor(snapshot[-1].item_id) if snapshot else resume_cursor)
        if not full_snapshot:
            raise AssetMigrationError("executable inventory is empty")
        full_snapshot = await self._preflight_inventory(full_snapshot)
        if resume_cursor is None:
            snapshot = full_snapshot
        else:
            positions = [index for index, item in enumerate(full_snapshot) if item.item_id == resume_cursor.item_id]
            snapshot = full_snapshot[positions[0] + 1 :]
        if not snapshot:
            await self._validate_catalog(full_snapshot)
            return MigrationResult(resume_cursor=resume_cursor)
        backup = create_secure_backup(snapshot, self.backup_root, keyring=self.keyring)
        created = 0
        noop = 0
        try:
            for offset in range(0, len(snapshot), batch_size):
                batch = snapshot[offset : offset + batch_size]
                async with self.session_factory() as session:
                    async with session.begin():
                        for item in batch:
                            effective_item = item
                            if item.kind == "skill":
                                item_created, item_noop = await self._migrate_skill(session, effective_item)
                            elif item.kind == "agent":
                                item_created, item_noop = await self._migrate_agent(session, effective_item)
                            elif item.kind == "mcp":
                                item_created, item_noop = await self._migrate_mcp(session, effective_item)
                            else:
                                raise AssetMigrationError("unsupported migration asset kind")
                            created += item_created
                            noop += item_noop
            await self._validate_catalog(full_snapshot)
        except Exception:
            _write_migration_status(
                backup,
                {
                    "status": "failed",
                    "created_versions": created,
                    "noop_versions": noop,
                },
            )
            raise
        _write_migration_status(
            backup,
            {
                "status": "completed",
                "created_versions": created,
                "noop_versions": noop,
                "resume_cursor": str(snapshot[-1].item_id),
            },
        )
        return MigrationResult(
            created_versions=created,
            noop_versions=noop,
            resume_cursor=MigrationCursor(snapshot[-1].item_id),
            run_id=backup.run_id,
        )


def _load_owner_map(path: Path | None, actor_user_id: str | None) -> OwnerMap:
    if path is None:
        return OwnerMap({}, system_actor=actor_user_id)
    try:
        raw = json.loads(_read_regular_file(path).decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        projects_raw = raw.get("default_projects", {})
        if not isinstance(projects_raw, dict):
            raise ValueError
        projects = {str(user_id): uuid.UUID(str(project_id)) for user_id, project_id in projects_raw.items()}
        legacy_owner = raw.get("legacy_shared_owner")
        system_actor = actor_user_id or raw.get("system_actor")
        if legacy_owner is not None and not isinstance(legacy_owner, str):
            raise ValueError
        if system_actor is not None and not isinstance(system_actor, str):
            raise ValueError
        return OwnerMap(projects, legacy_shared_owner=legacy_owner, system_actor=system_actor)
    except (AssetMigrationError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise AssetMigrationError("owner map is invalid") from None


async def _run_cli(args: argparse.Namespace, inventory: Sequence[InventoryItem], repo_root: Path) -> MigrationResult:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise AssetMigrationError("DATABASE_URL is required")
    has_secrets = any(item.kind == "mcp" and bool(item.payload.get("secret_payload")) for item in inventory)
    keyring = None
    if has_secrets:
        try:
            keyring = CredentialKeyring.from_environment()
        except CredentialKeyringInvalid:
            raise AssetMigrationError("active credential key is unavailable") from None
    config = DatabaseConfig(url=database_url)
    engine = create_async_engine(config.sqlalchemy_url)
    try:
        runner = AssetMigrationRunner(
            async_sessionmaker(engine, expire_on_commit=False),
            backup_root=repo_root / ".deer-flow/migrations/assets",
            keyring=keyring,
            actor_user_id=args.actor_user_id,
        )
        return await runner.run(
            inventory,
            execute=True,
            resume_cursor=args.resume_cursor,
            batch_size=args.batch_size,
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_migration_parser().parse_args(argv)
    try:
        repo_root = args.repo_root.absolute()
        data_root = resolve_data_root(repo_root, args.data_root)
        owners = _load_owner_map(args.owner_map, args.actor_user_id)
        inventory = build_inventory(
            SourceLayout(
                repo_root,
                data_root,
            ),
            owners,
        )
        print(render_inventory(inventory))
        validate_executable_inventory(inventory)
        if args.dry_run:
            print(json.dumps({"mode": "dry-run", "items": len(inventory)}, separators=(",", ":"), sort_keys=True))
            return 0
        result = asyncio.run(_run_cli(args, inventory, repo_root))
        print(
            json.dumps(
                {
                    "mode": "execute",
                    "run_id": str(result.run_id) if result.run_id else None,
                    "created_versions": result.created_versions,
                    "noop_versions": result.noop_versions,
                    "resume_cursor": str(result.resume_cursor.item_id) if result.resume_cursor else None,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except AssetMigrationError:
        print("asset migration failed safely", file=sys.stderr)
        return 1
    except Exception:
        print("asset migration failed safely", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
