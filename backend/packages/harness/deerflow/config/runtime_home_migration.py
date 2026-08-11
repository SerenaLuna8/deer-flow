"""Safe, explicit migration of a local legacy runtime home.

The migration is deliberately copy-only.  It never removes the source, never
merges into an existing target, and publishes a verified staging directory with
an atomic no-replace rename.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class RuntimeHomeMigrationError(RuntimeError):
    """A safe refusal to plan or copy a local runtime home."""


@dataclass(frozen=True, slots=True)
class RuntimeHomeManifestEntry:
    """One deterministic directory-tree manifest entry."""

    relative_path: str
    kind: Literal["directory", "file"]
    size: int
    sha256: str | None
    mode: int


@dataclass(frozen=True, slots=True)
class RuntimeHomeSourceSnapshot:
    """One validated legacy source and its immutable plan-time snapshot."""

    source: Path
    manifest: tuple[RuntimeHomeManifestEntry, ...]
    source_mode: int


@dataclass(frozen=True, slots=True)
class RuntimeHomeMigrationPlan:
    """Validated source snapshots, their union, and an absent destination."""

    source_snapshots: tuple[RuntimeHomeSourceSnapshot, ...]
    target: Path
    manifest: tuple[RuntimeHomeManifestEntry, ...]
    entry_sources: tuple[Path, ...]
    source_mode: int

    @property
    def sources(self) -> tuple[Path, ...]:
        return tuple(snapshot.source for snapshot in self.source_snapshots)

    @property
    def file_count(self) -> int:
        return sum(entry.kind == "file" for entry in self.manifest)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.manifest if entry.kind == "file")


@dataclass(frozen=True, slots=True)
class RuntimeHomeMigrationResult:
    """Dry-run or verified-copy outcome suitable for operator output."""

    sources: tuple[Path, ...]
    target: Path
    file_count: int
    total_bytes: int
    applied: bool
    verified: bool


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _absolute_path(path: Path, *, relative_to: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = relative_to / expanded
    return Path(os.path.abspath(os.fspath(expanded)))


def _validated_repository_root(repository_root: Path) -> Path:
    root = Path(repository_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise RuntimeHomeMigrationError("仓库根路径不是目录")
    return root


def _select_sources(repository_root: Path, source: Path | Sequence[Path] | None) -> tuple[Path, ...]:
    if source is None:
        candidates = (
            repository_root / ".deer-flow",
            repository_root / "backend" / ".deer-flow",
        )
        selected = tuple(candidate for candidate in candidates if _lexists(candidate))
    elif isinstance(source, Path):
        selected = (_absolute_path(source, relative_to=repository_root),)
    else:
        selected = tuple(_absolute_path(Path(candidate), relative_to=repository_root) for candidate in source)

    if not selected:
        raise RuntimeHomeMigrationError(
            "未找到旧运行目录（.deer-flow 或 backend/.deer-flow）；请通过 --source 指定源目录",
        )
    return selected


def _validate_source(source: Path) -> tuple[Path, int]:
    try:
        source_stat = os.lstat(source)
    except FileNotFoundError:
        raise RuntimeHomeMigrationError("源运行目录不存在") from None
    except OSError as exc:
        raise RuntimeHomeMigrationError("无法检查源运行目录") from exc
    if stat.S_ISLNK(source_stat.st_mode):
        raise RuntimeHomeMigrationError("源运行目录不得是符号链接")
    if not stat.S_ISDIR(source_stat.st_mode):
        raise RuntimeHomeMigrationError("源运行路径必须是目录")
    resolved = source.resolve(strict=True)
    return resolved, stat.S_IMODE(source_stat.st_mode)


def _validate_target(repository_root: Path, target: Path | None) -> Path:
    lexical_target = _absolute_path(target or Path(".act-weave"), relative_to=repository_root)
    if _lexists(lexical_target):
        raise RuntimeHomeMigrationError("目标路径已存在；拒绝覆盖或合并")
    try:
        target_parent = lexical_target.parent.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise RuntimeHomeMigrationError("目标路径的父目录不存在") from None
    if not target_parent.is_dir():
        raise RuntimeHomeMigrationError("目标路径的父路径不是目录")
    normalized_target = target_parent / lexical_target.name
    if _lexists(normalized_target):
        raise RuntimeHomeMigrationError("目标路径已存在；拒绝覆盖或合并")
    return normalized_target


def _paths_are_nested(source: Path, target: Path) -> bool:
    return source == target or source.is_relative_to(target) or target.is_relative_to(source)


def _hash_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeHomeMigrationError("无法安全读取源目录中的常规文件") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "rb") as source_file:
            opened_stat = os.fstat(source_file.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise RuntimeHomeMigrationError("源目录在扫描期间出现符号链接或特殊文件")
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            final_stat = os.fstat(source_file.fileno())
    except Exception:
        raise
    if (opened_stat.st_dev, opened_stat.st_ino, opened_stat.st_size, opened_stat.st_mtime_ns) != (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_size,
        final_stat.st_mtime_ns,
    ):
        raise RuntimeHomeMigrationError("源目录在生成清单期间发生变化")
    if size != final_stat.st_size:
        raise RuntimeHomeMigrationError("源目录在生成清单期间发生变化")
    return size, digest.hexdigest()


def _build_manifest(root: Path) -> tuple[RuntimeHomeManifestEntry, ...]:
    """Build a deterministic SHA-256/size manifest without following links."""

    validated_root, _ = _validate_source(root)
    entries: list[RuntimeHomeManifestEntry] = []

    def scan(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeHomeMigrationError("无法扫描运行目录") from exc
        for child in children:
            child_path = Path(child.path)
            relative_path = child_path.relative_to(validated_root).as_posix()
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeHomeMigrationError("无法检查运行目录条目") from exc
            mode = stat.S_IMODE(child_stat.st_mode)
            if stat.S_ISLNK(child_stat.st_mode):
                raise RuntimeHomeMigrationError(f"源目录包含符号链接：{relative_path}")
            if stat.S_ISDIR(child_stat.st_mode):
                entries.append(
                    RuntimeHomeManifestEntry(
                        relative_path=relative_path,
                        kind="directory",
                        size=0,
                        sha256=None,
                        mode=mode,
                    )
                )
                scan(child_path)
                continue
            if stat.S_ISREG(child_stat.st_mode):
                size, digest = _hash_regular_file(child_path)
                entries.append(
                    RuntimeHomeManifestEntry(
                        relative_path=relative_path,
                        kind="file",
                        size=size,
                        sha256=digest,
                        mode=mode,
                    )
                )
                continue
            raise RuntimeHomeMigrationError(f"源目录包含特殊文件：{relative_path}")

    scan(validated_root)
    return tuple(sorted(entries, key=lambda entry: entry.relative_path))


def _snapshot_sources(
    repository_root: Path,
    source: Path | Sequence[Path] | None,
) -> tuple[RuntimeHomeSourceSnapshot, ...]:
    snapshots: list[RuntimeHomeSourceSnapshot] = []
    seen: set[Path] = set()
    for selected_source in _select_sources(repository_root, source):
        source_path, source_mode = _validate_source(selected_source)
        if source_path in seen:
            continue
        for snapshot in snapshots:
            if _paths_are_nested(source_path, snapshot.source):
                raise RuntimeHomeMigrationError("多个源运行目录不得相同或互相嵌套")
        seen.add(source_path)
        snapshots.append(
            RuntimeHomeSourceSnapshot(
                source=source_path,
                manifest=_build_manifest(source_path),
                source_mode=source_mode,
            )
        )
    if not snapshots:
        raise RuntimeHomeMigrationError("没有可迁移的唯一源运行目录")
    return tuple(snapshots)


def _merge_source_manifests(
    snapshots: tuple[RuntimeHomeSourceSnapshot, ...],
) -> tuple[tuple[RuntimeHomeManifestEntry, ...], tuple[Path, ...], int]:
    source_mode = snapshots[0].source_mode
    if any(snapshot.source_mode != source_mode for snapshot in snapshots[1:]):
        raise RuntimeHomeMigrationError("多个源运行目录的根权限冲突；拒绝猜测目标权限")

    merged: dict[str, tuple[RuntimeHomeManifestEntry, Path]] = {}
    for snapshot in snapshots:
        for entry in snapshot.manifest:
            existing = merged.get(entry.relative_path)
            if existing is None:
                merged[entry.relative_path] = (entry, snapshot.source)
                continue
            if existing[0] != entry:
                raise RuntimeHomeMigrationError(f"多个源运行目录存在冲突：{entry.relative_path}")

    ordered = tuple(merged[path] for path in sorted(merged))
    return (
        tuple(item[0] for item in ordered),
        tuple(item[1] for item in ordered),
        source_mode,
    )


def plan_runtime_home_migration(
    repository_root: Path,
    *,
    source: Path | Sequence[Path] | None = None,
    target: Path | None = None,
) -> RuntimeHomeMigrationPlan:
    """Validate paths and hash the source union without creating the target."""

    root = _validated_repository_root(repository_root)
    source_snapshots = _snapshot_sources(root, source)
    target_path = _validate_target(root, target)
    if any(_paths_are_nested(snapshot.source, target_path) for snapshot in source_snapshots):
        raise RuntimeHomeMigrationError("源目录与目标目录不得相同或互相嵌套")
    manifest, entry_sources, source_mode = _merge_source_manifests(source_snapshots)
    return RuntimeHomeMigrationPlan(
        source_snapshots=source_snapshots,
        target=target_path,
        manifest=manifest,
        entry_sources=entry_sources,
        source_mode=source_mode,
    )


def _copy_regular_file(source: Path, target: Path, mode: int) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_descriptor = os.open(source, flags)
    except OSError as exc:
        raise RuntimeHomeMigrationError("无法安全复制源目录中的常规文件") from exc
    try:
        with os.fdopen(source_descriptor, "rb") as source_file:
            if not stat.S_ISREG(os.fstat(source_file.fileno()).st_mode):
                raise RuntimeHomeMigrationError("源目录在复制期间出现符号链接或特殊文件")
            with target.open("xb") as target_file:
                shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
        os.chmod(target, mode, follow_symlinks=False)
    except Exception:
        raise


def _populate_staging(plan: RuntimeHomeMigrationPlan, staging: Path) -> None:
    directory_entries = tuple(entry for entry in plan.manifest if entry.kind == "directory")
    for entry in sorted(directory_entries, key=lambda item: item.relative_path.count("/")):
        (staging / entry.relative_path).mkdir()
    for entry, source in zip(plan.manifest, plan.entry_sources, strict=True):
        if entry.kind == "file":
            _copy_regular_file(
                source / entry.relative_path,
                staging / entry.relative_path,
                entry.mode,
            )
    for entry in sorted(directory_entries, key=lambda item: item.relative_path.count("/"), reverse=True):
        os.chmod(staging / entry.relative_path, entry.mode, follow_symlinks=False)
    os.chmod(staging, plan.source_mode, follow_symlinks=False)


def _raise_rename_error(error_number: int) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RuntimeHomeMigrationError("目标路径已出现；拒绝覆盖或合并")
    raise RuntimeHomeMigrationError(f"无法原子发布迁移目录：{os.strerror(error_number)}")


def _atomic_rename_no_replace(source: Path, target: Path) -> None:
    """Atomically rename a directory while refusing an existing target."""

    if _lexists(target):
        raise RuntimeHomeMigrationError("目标路径已出现；拒绝覆盖或合并")

    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename_exclusive = getattr(libc, "renamex_np", None)
        if rename_exclusive is None:
            raise RuntimeHomeMigrationError("当前平台缺少安全的原子 no-replace rename")
        rename_exclusive.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename_exclusive.restype = ctypes.c_int
        if rename_exclusive(source_bytes, target_bytes, 0x00000004) != 0:  # RENAME_EXCL
            _raise_rename_error(ctypes.get_errno())
        return

    if sys.platform.startswith("linux"):
        rename_no_replace = getattr(libc, "renameat2", None)
        if rename_no_replace is None:
            raise RuntimeHomeMigrationError("当前平台缺少安全的原子 no-replace rename")
        rename_no_replace.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_no_replace.restype = ctypes.c_int
        if rename_no_replace(-100, source_bytes, -100, target_bytes, 1) != 0:  # AT_FDCWD, RENAME_NOREPLACE
            _raise_rename_error(ctypes.get_errno())
        return

    if os.name == "nt":
        try:
            os.rename(source, target)
        except FileExistsError:
            raise RuntimeHomeMigrationError("目标路径已出现；拒绝覆盖或合并") from None
        except OSError as exc:
            if _lexists(target):
                raise RuntimeHomeMigrationError("目标路径已出现；拒绝覆盖或合并") from None
            raise RuntimeHomeMigrationError("无法原子发布迁移目录") from exc
        return

    raise RuntimeHomeMigrationError("当前平台缺少安全的原子 no-replace rename")


def _cleanup_staging(staging: Path) -> None:
    if not _lexists(staging):
        return
    try:
        if staging.is_symlink() or not staging.is_dir():
            staging.unlink()
        else:
            shutil.rmtree(staging)
    except OSError as exc:
        raise RuntimeHomeMigrationError("迁移失败，且无法清理本次创建的 staging 目录") from exc


def _result(plan: RuntimeHomeMigrationPlan, *, applied: bool, verified: bool) -> RuntimeHomeMigrationResult:
    return RuntimeHomeMigrationResult(
        sources=plan.sources,
        target=plan.target,
        file_count=plan.file_count,
        total_bytes=plan.total_bytes,
        applied=applied,
        verified=verified,
    )


def _sources_match_plan(plan: RuntimeHomeMigrationPlan) -> bool:
    for snapshot in plan.source_snapshots:
        source_path, source_mode = _validate_source(snapshot.source)
        if source_path != snapshot.source or source_mode != snapshot.source_mode:
            return False
        if _build_manifest(snapshot.source) != snapshot.manifest:
            return False
    return True


def copy_runtime_home(plan: RuntimeHomeMigrationPlan) -> RuntimeHomeMigrationResult:
    """Copy, verify, and atomically publish a previously validated plan."""

    if _lexists(plan.target):
        raise RuntimeHomeMigrationError("目标路径已存在；拒绝覆盖或合并")
    if not _sources_match_plan(plan):
        raise RuntimeHomeMigrationError("源目录在预演后发生变化；请重新执行迁移")

    staging = Path(
        tempfile.mkdtemp(
            prefix=f"{plan.target.name}.migration-",
            suffix=".staging",
            dir=plan.target.parent,
        )
    )
    try:
        _populate_staging(plan, staging)
        if not _sources_match_plan(plan):
            raise RuntimeHomeMigrationError("源目录在复制期间发生变化")
        if _build_manifest(staging) != plan.manifest:
            raise RuntimeHomeMigrationError("staging 目录清单校验失败")
        if _lexists(plan.target):
            raise RuntimeHomeMigrationError("目标路径已出现；拒绝覆盖或合并")
        _atomic_rename_no_replace(staging, plan.target)
    finally:
        _cleanup_staging(staging)

    return _result(plan, applied=True, verified=True)


def migrate_runtime_home(
    repository_root: Path,
    *,
    source: Path | Sequence[Path] | None = None,
    target: Path | None = None,
    apply: bool = False,
) -> RuntimeHomeMigrationResult:
    """Plan by default; copy only when ``apply`` is explicitly true."""

    plan = plan_runtime_home_migration(
        repository_root,
        source=source,
        target=target,
    )
    if apply:
        return copy_runtime_home(plan)
    return _result(plan, applied=False, verified=False)
