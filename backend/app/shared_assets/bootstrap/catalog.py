"""Strict loader for packaged canonical system assets."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from importlib import resources
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictInt,
    ValidationError,
    model_validator,
)

_PACKAGE = "app.shared_assets.bootstrap"
_SOURCE_KEY_PATTERN = r"^builtin:(agent|skill|mcp):[a-z0-9-]+$"
_SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"


class BootstrapCatalogError(ValueError):
    """The packaged catalog is malformed or its content is not authentic."""


class BootstrapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    kind: Literal["agent", "skill", "mcp"]
    slug: str = Field(pattern=_SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=120)
    version: StrictInt = Field(ge=1)
    payload_path: str
    payload_format: Literal["document", "skill_archive_v1"] = "document"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _source_kind_matches(self) -> BootstrapEntry:
        if self.source_key.split(":", 2)[1] != self.kind:
            raise ValueError("bootstrap source key kind does not match entry kind")
        if self.payload_format == "skill_archive_v1" and self.kind != "skill":
            raise ValueError("bootstrap Skill archives require Skill entries")
        _safe_relative_path(self.payload_path)
        return self


class BootstrapCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = Field(ge=1, le=3)
    entries: tuple[BootstrapEntry, ...]
    _payloads: MappingProxyType = PrivateAttr(default_factory=lambda: MappingProxyType({}))
    _digest: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def _unique_entries(self) -> BootstrapCatalog:
        if self.schema_version == 1:
            source_keys = [entry.source_key for entry in self.entries]
            if len(source_keys) != len(set(source_keys)):
                raise ValueError("bootstrap schema v1 source keys must be unique")
        release_keys = [(entry.source_key, entry.version) for entry in self.entries]
        if len(release_keys) != len(set(release_keys)):
            raise ValueError("bootstrap release keys must be unique")
        payload_paths = [entry.payload_path for entry in self.entries]
        if len(payload_paths) != len(set(payload_paths)):
            raise ValueError("bootstrap payload paths must be unique")

        histories: dict[str, list[BootstrapEntry]] = {}
        slug_owners: dict[tuple[str, str], str] = {}
        for entry in self.entries:
            histories.setdefault(entry.source_key, []).append(entry)
            slug_key = (entry.kind, entry.slug.casefold())
            owner = slug_owners.setdefault(slug_key, entry.source_key)
            if owner != entry.source_key:
                raise ValueError("bootstrap slugs must be unique within each kind")

        for history in histories.values():
            history.sort(key=lambda entry: entry.version)
            first = history[0]
            canonical_metadata = (
                first.kind,
                first.slug,
                first.display_name,
                first.payload_format,
            )
            if any(
                (
                    entry.kind,
                    entry.slug,
                    entry.display_name,
                    entry.payload_format,
                )
                != canonical_metadata
                for entry in history
            ):
                raise ValueError("bootstrap release metadata must remain stable")
            if first.kind in {"agent", "skill"} and (len(history) != 1 or first.version != 1):
                raise ValueError(f"bootstrap {first.kind.title()} assets require one v1 definition")
            if first.kind == "mcp" and (len(history) != 1 or first.version != 1):
                raise ValueError("bootstrap MCP assets currently require one v1 release")
        return self


def _safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BootstrapCatalogError("bootstrap payload path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise BootstrapCatalogError("bootstrap payload path is invalid")
    return relative


def _package_root() -> Path:
    traversable = resources.files(_PACKAGE)
    try:
        return Path(os.fspath(traversable))
    except TypeError:
        raise BootstrapCatalogError("bootstrap package must be installed as regular files") from None


def _require_real_root(root: Path) -> None:
    try:
        root_mode = os.lstat(root).st_mode
    except OSError:
        raise BootstrapCatalogError("bootstrap package root is unavailable") from None
    if stat.S_ISLNK(root_mode):
        raise BootstrapCatalogError("bootstrap path contains a symlink")
    if not stat.S_ISDIR(root_mode):
        raise BootstrapCatalogError("bootstrap package root is invalid")


def require_real_directory_beneath(root: Path, relative_value: str) -> Path:
    """Return a real directory below ``root`` without following symlinks."""

    relative = _safe_relative_path(relative_value)
    _require_real_root(root)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            raise BootstrapCatalogError("bootstrap directory is unavailable") from None
        if stat.S_ISLNK(mode):
            raise BootstrapCatalogError("bootstrap path contains a symlink")
        if not stat.S_ISDIR(mode):
            raise BootstrapCatalogError("bootstrap path must contain real directories")
    return current


def ensure_real_directory_beneath(root: Path, relative_value: str) -> Path:
    """Create missing directory components below a real, non-symlink root."""

    relative = _safe_relative_path(relative_value)
    _require_real_root(root)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            try:
                current.mkdir()
                mode = os.lstat(current).st_mode
            except OSError:
                raise BootstrapCatalogError("bootstrap directory is unavailable") from None
        except OSError:
            raise BootstrapCatalogError("bootstrap directory is unavailable") from None
        if stat.S_ISLNK(mode):
            raise BootstrapCatalogError("bootstrap path contains a symlink")
        if not stat.S_ISDIR(mode):
            raise BootstrapCatalogError("bootstrap path must contain real directories")
    return current


def read_regular_file_beneath(root: Path, relative_value: str) -> bytes:
    """Read a regular file below ``root`` after checking every path component."""

    relative = _safe_relative_path(relative_value)
    _require_real_root(root)
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            raise BootstrapCatalogError("bootstrap payload is unavailable") from None
        if stat.S_ISLNK(mode):
            raise BootstrapCatalogError("bootstrap payload path contains a symlink")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
            raise BootstrapCatalogError("bootstrap payload path is invalid")
    if not stat.S_ISREG(mode):
        raise BootstrapCatalogError("bootstrap payload must be a regular file")
    try:
        return current.read_bytes()
    except OSError:
        raise BootstrapCatalogError("bootstrap payload is unavailable") from None


def _catalog_digest(catalog: BootstrapCatalog) -> str:
    canonical = json.dumps(
        catalog.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def load_bootstrap_catalog() -> BootstrapCatalog:
    """Load and authenticate the packaged manifest and every listed payload."""

    root = _package_root()
    try:
        raw_manifest = read_regular_file_beneath(root, "catalog.json")
        decoded = json.loads(raw_manifest)
        catalog = BootstrapCatalog.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as error:
        raise BootstrapCatalogError("bootstrap catalog is invalid") from error

    payloads: dict[tuple[str, int], bytes] = {}
    for entry in catalog.entries:
        content = read_regular_file_beneath(root, entry.payload_path)
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise BootstrapCatalogError("bootstrap payload digest mismatch")
        payloads[(entry.source_key, entry.version)] = content
    catalog._payloads = MappingProxyType(payloads)
    catalog._digest = _catalog_digest(catalog)
    return catalog


def catalog_payload(catalog: BootstrapCatalog, entry: BootstrapEntry) -> bytes:
    try:
        return catalog._payloads[(entry.source_key, entry.version)]
    except KeyError:
        raise BootstrapCatalogError("bootstrap payload was not authenticated") from None


def catalog_digest(catalog: BootstrapCatalog) -> str:
    if not catalog._digest:
        raise BootstrapCatalogError("bootstrap catalog was not loaded by the canonical loader")
    return catalog._digest


__all__ = [
    "BootstrapCatalog",
    "BootstrapCatalogError",
    "BootstrapEntry",
    "ensure_real_directory_beneath",
    "load_bootstrap_catalog",
    "read_regular_file_beneath",
    "require_real_directory_beneath",
]
