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

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

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
    version: int = Field(ge=1)
    payload_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _source_kind_matches(self) -> BootstrapEntry:
        if self.source_key.split(":", 2)[1] != self.kind:
            raise ValueError("bootstrap source key kind does not match entry kind")
        _safe_relative_path(self.payload_path)
        return self


class BootstrapCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    entries: tuple[BootstrapEntry, ...]
    _payloads: MappingProxyType = PrivateAttr(default_factory=lambda: MappingProxyType({}))
    _digest: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def _unique_entries(self) -> BootstrapCatalog:
        source_keys = [entry.source_key for entry in self.entries]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("bootstrap source keys must be unique")
        kind_slugs = [(entry.kind, entry.slug.casefold()) for entry in self.entries]
        if len(kind_slugs) != len(set(kind_slugs)):
            raise ValueError("bootstrap slugs must be unique within each kind")
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


def _read_regular_file(root: Path, relative_value: str) -> bytes:
    relative = _safe_relative_path(relative_value)
    root_mode = os.lstat(root).st_mode
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise BootstrapCatalogError("bootstrap package root is invalid")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            raise BootstrapCatalogError("bootstrap payload is unavailable") from None
        if stat.S_ISLNK(mode):
            raise BootstrapCatalogError("bootstrap payload path contains a symlink")
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
        raw_manifest = _read_regular_file(root, "catalog.json")
        decoded = json.loads(raw_manifest)
        catalog = BootstrapCatalog.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as error:
        raise BootstrapCatalogError("bootstrap catalog is invalid") from error

    payloads: dict[str, bytes] = {}
    for entry in catalog.entries:
        content = _read_regular_file(root, entry.payload_path)
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise BootstrapCatalogError("bootstrap payload digest mismatch")
        payloads[entry.source_key] = content
    catalog._payloads = MappingProxyType(payloads)
    catalog._digest = _catalog_digest(catalog)
    return catalog


def catalog_payload(catalog: BootstrapCatalog, entry: BootstrapEntry) -> bytes:
    try:
        return catalog._payloads[entry.source_key]
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
    "load_bootstrap_catalog",
]
