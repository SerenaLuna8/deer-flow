"""Standard-library helpers for ActWeave native runtime path aliases.

This module intentionally has no project dependencies.  Setup and diagnostic
scripts import it before ``uv sync`` has necessarily populated the backend
environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


class LocalRuntimePathConflict(ValueError):
    """Two names for one local path resolve to different locations."""


def _configured_value(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name)
    return value if value else None


def normalize_local_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """Return an absolute, normalized local path without requiring it to exist."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve(strict=False)


def resolve_environment_path(
    canonical_name: str,
    legacy_name: str,
    *,
    environment: Mapping[str, str] | None = None,
    default: str | os.PathLike[str] | None = None,
    base: Path | None = None,
) -> Path | None:
    """Resolve a canonical/legacy environment alias pair.

    ``canonical_name`` is preferred, but the legacy name remains accepted.  If
    callers supply both names, their normalized paths must agree; silently
    choosing one would risk reading or writing a different runtime directory.
    """
    values = os.environ if environment is None else environment
    canonical_value = _configured_value(values, canonical_name)
    legacy_value = _configured_value(values, legacy_name)
    canonical_path = normalize_local_path(canonical_value, base=base) if canonical_value else None
    legacy_path = normalize_local_path(legacy_value, base=base) if legacy_value else None
    if canonical_path is not None and legacy_path is not None and canonical_path != legacy_path:
        raise LocalRuntimePathConflict(f"{canonical_name} resolves to '{canonical_path}', but {legacy_name} resolves to '{legacy_path}'; refusing conflicting local runtime paths")
    if canonical_path is not None:
        return canonical_path
    if legacy_path is not None:
        return legacy_path
    if default is None:
        return None
    return normalize_local_path(default, base=base)
