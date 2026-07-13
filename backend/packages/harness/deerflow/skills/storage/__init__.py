"""SkillStorage singleton + reflection-based factory.

Mirrors the pattern used by ``deerflow/sandbox/sandbox_provider.py``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from deerflow.skills.storage.local_skill_storage import LocalSkillStorage
from deerflow.skills.storage.skill_storage import SkillStorage
from deerflow.skills.storage.user_scoped_skill_storage import UserScopedSkillStorage
from deerflow.skills.types import SecretRequirement, Skill, SkillCategory

logger = logging.getLogger(__name__)

_default_skill_storage: SkillStorage | None = None
_default_skill_storage_config: object | None = None  # AppConfig identity the singleton was built from
_skill_storage_lock = threading.Lock()

# Maximum number of per-user storage instances to keep in cache.
# Real-world deployments rarely have more than a few concurrent users per
# process; 64 is a generous ceiling that prevents unbounded memory growth.
_MAX_USER_SCOPED_STORAGES = 64

# Per-user skill storage cache with double-check lock for concurrent creation.
# OrderedDict so that LRU eviction can remove the least-recently-used entry
# via ``move_to_end`` + ``popitem(last=False)`` when the cache exceeds
# ``_MAX_USER_SCOPED_STORAGES``.
_user_scoped_storages: OrderedDict[str, UserScopedSkillStorage] = OrderedDict()
_user_scoped_storage_lock = threading.Lock()
_catalog_skill_materialization_lock = threading.Lock()
_CATALOG_SKILL_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _catalog_skill_path(path: str):
    from deerflow.assets.catalog import AssetCatalogUnavailable

    relative = PurePosixPath(path)
    if not isinstance(path, str) or not path or "\\" in path or relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise AssetCatalogUnavailable("system skill snapshot path is invalid")
    return relative


def _reject_catalog_symlink(path: Path) -> None:
    from deerflow.assets.catalog import AssetCatalogUnavailable

    if path.is_symlink():
        raise AssetCatalogUnavailable("system skill materialization root is invalid")


def get_catalog_skills_if_cutover(app_config=None) -> list[Skill] | None:
    """Return PostgreSQL system skills after cutover, otherwise ``None``.

    The adapter constructs only immutable skill metadata. Snapshot bytes stay
    inside the provider result and are never read from a legacy file path.
    """

    from deerflow.assets.catalog import (
        AssetCatalogSkillSnapshot,
        AssetCatalogUnavailable,
        get_asset_catalog_provider,
        require_system_asset,
        run_asset_catalog_lookup,
    )

    provider = get_asset_catalog_provider()
    if provider is None or not run_asset_catalog_lookup(provider, "is_cutover_enabled"):
        return None
    snapshots = run_asset_catalog_lookup(provider, "list_system_skills")
    if not isinstance(snapshots, tuple):
        raise AssetCatalogUnavailable("system skill catalog is invalid")
    if not snapshots:
        raise AssetCatalogUnavailable("published system skill catalog is empty")
    for snapshot in snapshots:
        if not isinstance(snapshot, AssetCatalogSkillSnapshot):
            raise AssetCatalogUnavailable("system skill snapshot is invalid")
        require_system_asset(snapshot)
        if not _CATALOG_SKILL_SLUG.fullmatch(snapshot.slug):
            raise AssetCatalogUnavailable("system skill snapshot slug is invalid")
        if not snapshot.files or not any(file.path == "SKILL.md" for file in snapshot.files):
            raise AssetCatalogUnavailable("system skill snapshot is invalid")
        for file in snapshot.files:
            _catalog_skill_path(file.path)
    generations = {snapshot.generation for snapshot in snapshots if isinstance(snapshot, AssetCatalogSkillSnapshot)}
    if len(generations) != 1:
        raise AssetCatalogUnavailable("system skill catalog generation is invalid")
    generation = generations.pop()
    if type(generation) is not int or generation < 0:
        raise AssetCatalogUnavailable("system skill catalog generation is invalid")

    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()
    skills_root = app_config.skills.get_skills_path()
    custom_root = skills_root / "custom"
    managed_root = custom_root / ".asset-catalog"
    generation_root = managed_root / str(generation)

    with _catalog_skill_materialization_lock:
        _reject_catalog_symlink(skills_root)
        _reject_catalog_symlink(custom_root)
        _reject_catalog_symlink(managed_root)
        custom_root.mkdir(parents=True, exist_ok=True)
        managed_root.mkdir(exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix=f".{generation}.staging-", dir=managed_root))
        try:
            for snapshot in snapshots:
                skill_root = staging_root / snapshot.slug
                for file in snapshot.files:
                    relative = _catalog_skill_path(file.path)
                    target = skill_root.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(file.content)
            previous_root = managed_root / f".{generation}.previous-{uuid.uuid4().hex}"
            if generation_root.exists():
                _reject_catalog_symlink(generation_root)
                os.replace(generation_root, previous_root)
            os.replace(staging_root, generation_root)
            if previous_root.exists():
                shutil.rmtree(previous_root)
            for child in managed_root.iterdir():
                if child != generation_root:
                    _reject_catalog_symlink(child)
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)

    skills: list[Skill] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, AssetCatalogSkillSnapshot):
            raise AssetCatalogUnavailable("system skill snapshot is invalid")
        require_system_asset(snapshot)
        if not any(file.path == "SKILL.md" for file in snapshot.files):
            raise AssetCatalogUnavailable("system skill snapshot is invalid")
        skill_dir = generation_root / snapshot.slug
        skills.append(
            Skill(
                name=snapshot.slug,
                description=snapshot.description,
                license=None,
                skill_dir=skill_dir,
                skill_file=skill_dir / "SKILL.md",
                relative_path=Path(".asset-catalog") / str(generation) / snapshot.slug,
                category=SkillCategory.PUBLIC,
                enabled=True,
                required_secrets=tuple(SecretRequirement(name=name) for name in snapshot.secret_requirements),
            )
        )
    return skills


def get_or_new_skill_storage(**kwargs) -> SkillStorage:
    """Return a ``SkillStorage`` instance — either a new one or the process singleton.

    **New instance** is created (never cached) when:
    - ``skills_path`` is provided — uses it as the ``host_path`` override (class still resolved via config).
    - ``app_config`` is provided — constructs a storage from ``app_config.skills``
      so that per-request config (e.g. Gateway ``Depends(get_config)``) is respected
      without polluting the process-level singleton.

    **Singleton** is returned (created on first call, then reused) when neither
    ``skills_path`` nor ``app_config`` is given — uses ``get_app_config()`` to
    resolve the active configuration.

    This singleton is used for reading **public** skills (global, read-only).
    For user-scoped custom skill operations, use
    :func:`get_or_new_user_skill_storage` instead.
    """
    global _default_skill_storage, _default_skill_storage_config

    from deerflow.config import get_app_config
    from deerflow.config.skills_config import SkillsConfig

    def _make_storage(skills_config: SkillsConfig, *, host_path: str | None = None, **kwargs) -> SkillStorage:
        from deerflow.reflection import resolve_class

        cls = resolve_class(skills_config.use, SkillStorage)
        return cls(
            host_path=host_path if host_path is not None else str(skills_config.get_skills_path()),
            container_path=skills_config.container_path,
            **kwargs,
        )

    skills_path = kwargs.pop("skills_path", None)
    app_config = kwargs.pop("app_config", None)

    if skills_path is not None:
        if app_config is not None:
            return _make_storage(app_config.skills, host_path=str(skills_path), **kwargs)
        # No app_config: use a default SkillsConfig so we never need to read config.yaml
        # when the caller has already supplied an explicit host path.
        from deerflow.config.skills_config import SkillsConfig

        return _make_storage(SkillsConfig(), host_path=str(skills_path), **kwargs)

    if app_config is not None:
        return _make_storage(app_config.skills, **kwargs)

    # If the singleton was manually injected (e.g. in tests) without a config
    # identity (_default_skill_storage_config is None), skip get_app_config()
    # entirely to avoid requiring a config.yaml on disk.
    if _default_skill_storage is not None and _default_skill_storage_config is None:
        return _default_skill_storage

    app_config_now = get_app_config()

    # Build the singleton under the lock with a double-check so racing cold-start
    # callers construct exactly one instance, and reset_skill_storage() can't null
    # the global out from under a concurrent read. We construct *inside* the lock
    # — mirroring get_memory_storage() rather than sandbox_provider's build-outside-
    # then-discard-the-loser — because SkillStorage has no teardown hook, so an
    # orphaned instance from a losing racer could not be cleaned up.
    with _skill_storage_lock:
        if _default_skill_storage is None or _default_skill_storage_config is not app_config_now:
            _default_skill_storage = _make_storage(app_config_now.skills, **kwargs)
            _default_skill_storage_config = app_config_now
        return _default_skill_storage


def get_or_new_user_skill_storage(user_id: str, **kwargs) -> SkillStorage:
    """Return a per-user ``SkillStorage`` instance for custom skill isolation.

    Uses :class:`UserScopedSkillStorage` which redirects custom skill paths
    to ``{base_dir}/users/{user_id}/skills/custom/`` while keeping public
    skill reads from the global root.

    ``user_id`` is normalised via :func:`make_safe_user_id` so that external
    identities (e.g. IM channel ids containing non-``[A-Za-z0-9_-]`` chars)
    are safely bucketed before reaching :class:`UserScopedSkillStorage`, which
    calls :func:`_validate_user_id` internally.

    Instances are cached by the *normalised* ``user_id`` with double-check
    locking to prevent concurrent creation races. When the cache exceeds
    ``_MAX_USER_SCOPED_STORAGES``, the least-recently-accessed entry is
    evicted (true LRU, not FIFO).
    """
    from deerflow.config.paths import make_safe_user_id

    safe_id = make_safe_user_id(user_id)

    # Always acquire lock so move_to_end is safe — makes this a true LRU
    # cache instead of FIFO. The overhead is negligible since dict ops are
    # fast and this function is called once per agent-creation cycle.
    with _user_scoped_storage_lock:
        cached = _user_scoped_storages.get(safe_id)
        if cached is not None:
            _user_scoped_storages.move_to_end(safe_id)
            return cached

        cached = UserScopedSkillStorage(safe_id, **kwargs)
        _user_scoped_storages[safe_id] = cached
        # Evict least-recently-used entry if cache exceeds the ceiling.
        # Since we just moved the current user_id to the end, popitem(last=False)
        # will evict the oldest/least-recently-accessed entry (never the
        # one we just created).
        while len(_user_scoped_storages) > _MAX_USER_SCOPED_STORAGES:
            evicted_key, evicted_val = _user_scoped_storages.popitem(last=False)
            logger.info("Evicted user-scoped skill storage for safe_id=%s (cache ceiling %d)", evicted_key, _MAX_USER_SCOPED_STORAGES)
        return cached


def user_should_see_legacy_skills(user_id: str, **kwargs) -> bool:
    """Return whether discovery exposes any LEGACY skills for this user.

    Sandbox mounts must not be more permissive than skill discovery. This
    helper centralizes that contract so local, AIO, and remote providers all
    follow the same visibility rule.
    """
    if kwargs:
        from deerflow.config.paths import make_safe_user_id

        storage = UserScopedSkillStorage(make_safe_user_id(user_id), **kwargs)
    else:
        storage = get_or_new_user_skill_storage(user_id)
    return any((skill.category.value if hasattr(skill.category, "value") else skill.category) == SkillCategory.LEGACY.value for skill in storage.load_skills(enabled_only=False))


def reset_skill_storage() -> None:
    """Clear all cached storage instances (used in tests and hot-reload scenarios)."""
    global _default_skill_storage, _default_skill_storage_config
    with _skill_storage_lock:
        _default_skill_storage = None
        _default_skill_storage_config = None
    with _user_scoped_storage_lock:
        _user_scoped_storages.clear()


def reset_user_skill_storage(user_id: str | None = None) -> None:
    """Clear per-user skill storage cache for a specific user, or all users.

    ``user_id`` is normalised via :func:`make_safe_user_id` so that the
    cache key matches the one used by :func:`get_or_new_user_skill_storage`.
    Without normalisation, IM-channel user IDs (e.g. ``feishu:xxx``) would
    fail to clear their stale cache entries.

    Args:
        user_id: If provided, remove only that user's cached storage.
            If ``None``, clear the entire per-user cache.
    """
    from deerflow.config.paths import make_safe_user_id

    with _user_scoped_storage_lock:
        if user_id is not None:
            safe_id = make_safe_user_id(user_id)
            _user_scoped_storages.pop(safe_id, None)
        else:
            _user_scoped_storages.clear()


__all__ = [
    "LocalSkillStorage",
    "SkillStorage",
    "UserScopedSkillStorage",
    "get_or_new_skill_storage",
    "get_or_new_user_skill_storage",
    "get_catalog_skills_if_cutover",
    "user_should_see_legacy_skills",
    "reset_skill_storage",
    "reset_user_skill_storage",
]
