#!/usr/bin/env python3
"""Generate authenticated packaged system Skill archives from skills/public."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath

from app.shared_assets.bootstrap.catalog import (
    BootstrapCatalog,
    ensure_real_directory_beneath,
    read_regular_file_beneath,
    require_real_directory_beneath,
)
from app.shared_assets.bootstrap.skill_archive import (
    dump_skill_archive,
    load_skill_archive,
)
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.skill_service import _analyze_skill_files
from scripts.import_project_skills import ProjectSkillImportError, load_project_skill_sources

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPOSITORY_ROOT / "skills" / "public"
_BOOTSTRAP_ROOT = _REPOSITORY_ROOT / "backend" / "app" / "shared_assets" / "bootstrap"
_CATALOG_PATH = _BOOTSTRAP_ROOT / "catalog.json"
_OUTPUT_ROOT = _BOOTSTRAP_ROOT / "content" / "public-skills"
_PAYLOAD_PREFIX = "content/public-skills/"
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _skill_preview(files):
    try:
        preview = _analyze_skill_files(
            tuple(files),
            "public-system-skill-catalog",
        )
    except AssetValidationFailed:
        raise ValueError("public Skill frontmatter is invalid") from None
    name = preview.frontmatter.get("name")
    if not isinstance(name, str) or _SLUG.fullmatch(name) is None:
        raise ValueError("public Skill name is invalid")
    return name, preview


def _scan_snapshot(preview) -> tuple[str, dict[str, object]]:
    return preview.scan_decision, dict(preview.scan_summary)


def _released_skill_histories(
    raw_entries: list[object],
) -> dict[str, list[dict[str, object]]]:
    histories: dict[str, list[dict[str, object]]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("system asset catalog entries must be objects")
        if raw_entry.get("kind") != "skill":
            continue
        entry = dict(raw_entry)
        name = entry.get("slug")
        version = entry.get("version")
        source_key = entry.get("source_key")
        payload_path = entry.get("payload_path")
        if (
            not isinstance(name, str)
            or _SLUG.fullmatch(name) is None
            or type(version) is not int
            or version < 1
            or source_key != f"builtin:skill:{name}"
            or entry.get("display_name") != name
            or entry.get("payload_format") != "skill_archive_v1"
            or not isinstance(payload_path, str)
            or payload_path != f"{_PAYLOAD_PREFIX}{name}-v{version}.skill.json"
        ):
            raise ValueError("released system Skill metadata is invalid")
        histories.setdefault(name, []).append(entry)

    for name, history in histories.items():
        history.sort(key=lambda entry: int(entry["version"]))
        versions = [int(entry["version"]) for entry in history]
        if versions != list(range(1, len(history) + 1)):
            raise ValueError(f"released system Skill history is not contiguous: {name}")
    return histories


def _released_archive(entry: dict[str, object]) -> bytes:
    relative_value = entry["payload_path"]
    expected_digest = entry.get("sha256")
    if not isinstance(relative_value, str) or not isinstance(expected_digest, str):
        raise ValueError("released system Skill payload metadata is invalid")
    relative = PurePosixPath(relative_value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or not relative_value.startswith(_PAYLOAD_PREFIX):
        raise ValueError("released system Skill payload path is invalid")
    content = read_regular_file_beneath(_BOOTSTRAP_ROOT, relative.as_posix())
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ValueError("released system Skill payload digest mismatch")
    try:
        load_skill_archive(content)
    except ValueError:
        raise ValueError("released system Skill archive is invalid") from None
    return content


def _retained_payload(entry: dict[str, object]) -> bytes:
    relative_value = entry.get("payload_path")
    expected_digest = entry.get("sha256")
    if not isinstance(relative_value, str) or not isinstance(expected_digest, str):
        raise ValueError("retained system asset payload metadata is invalid")
    relative = PurePosixPath(relative_value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("retained system asset payload path is invalid")
    content = read_regular_file_beneath(_BOOTSTRAP_ROOT, relative.as_posix())
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ValueError("retained system asset payload digest mismatch")
    return content


def _new_skill_entry(
    name: str,
    version: int,
    archive: bytes,
    *,
    scan_decision: str,
    scan_summary: dict[str, object],
) -> dict[str, object]:
    relative_path = f"{_PAYLOAD_PREFIX}{name}-v{version}.skill.json"
    return {
        "source_key": f"builtin:skill:{name}",
        "kind": "skill",
        "slug": name,
        "display_name": name,
        "version": version,
        "payload_path": relative_path,
        "payload_format": "skill_archive_v1",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "scan_decision": scan_decision,
        "scan_summary": scan_summary,
    }


def _expected_outputs() -> tuple[bytes, dict[str, bytes]]:
    try:
        catalog_relative = _CATALOG_PATH.relative_to(_BOOTSTRAP_ROOT).as_posix()
        raw_catalog = json.loads(
            read_regular_file_beneath(
                _BOOTSTRAP_ROOT,
                catalog_relative,
            )
        )
    except (OSError, ValueError):
        raise ValueError("system asset catalog path is invalid") from None
    if not isinstance(raw_catalog, dict) or raw_catalog.get("schema_version") not in {
        1,
        2,
        3,
    }:
        raise ValueError("system asset catalog schema is unsupported")
    raw_entries = raw_catalog.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("system asset catalog entries are invalid")
    BootstrapCatalog.model_validate(raw_catalog)
    retained_entries = [dict(entry) for entry in raw_entries if isinstance(entry, dict) and entry.get("kind") != "skill"]
    if len(retained_entries) != sum(isinstance(entry, dict) and entry.get("kind") != "skill" for entry in raw_entries):
        raise ValueError("system asset catalog entries are invalid")
    histories = _released_skill_histories(raw_entries)
    generated_entries: list[dict[str, object]] = []
    for entry in retained_entries:
        _retained_payload(entry)
    payloads: dict[str, bytes] = {}
    seen_names: set[str] = set()

    sources = load_project_skill_sources(_SOURCE_ROOT)
    for source in sources:
        name, preview = _skill_preview(source.files)
        if name in seen_names:
            raise ValueError("public Skill names must be unique")
        seen_names.add(name)
        archive = dump_skill_archive(source.files)
        scan_decision, scan_summary = _scan_snapshot(preview)
        history = histories.get(name, [])
        for entry in history:
            payloads[str(entry["payload_path"])] = _released_archive(entry)
            generated_entries.append(entry)
        latest = history[-1] if history else None
        archive_digest = hashlib.sha256(archive).hexdigest()
        latest_has_snapshot = latest is not None and latest.get("scan_decision") is not None
        latest_snapshot_matches = latest_has_snapshot and latest.get("scan_decision") == scan_decision and latest.get("scan_summary") == scan_summary
        if latest is None or latest["sha256"] != archive_digest or (latest_has_snapshot and not latest_snapshot_matches):
            next_version = 1 if latest is None else int(latest["version"]) + 1
            entry = _new_skill_entry(
                name,
                next_version,
                archive,
                scan_decision=scan_decision,
                scan_summary=scan_summary,
            )
            payloads[str(entry["payload_path"])] = archive
            generated_entries.append(entry)

    removed_names = sorted(set(histories) - seen_names)
    if removed_names:
        raise ValueError("released system Skills cannot be removed implicitly: " + ", ".join(removed_names))

    kind_order = {"skill": 0, "mcp": 1, "agent": 2}
    entries = sorted(
        [*retained_entries, *generated_entries],
        key=lambda entry: (
            kind_order[str(entry["kind"])],
            str(entry["source_key"]),
            int(entry["version"]),
        ),
    )
    catalog = {
        "schema_version": 3,
        "entries": entries,
    }
    BootstrapCatalog.model_validate(catalog)
    catalog_bytes = (
        json.dumps(
            catalog,
            ensure_ascii=False,
            indent=2,
        ).encode()
        + b"\n"
    )
    return catalog_bytes, payloads


def _check(catalog_bytes: bytes, payloads: dict[str, bytes]) -> bool:
    try:
        output_relative = _OUTPUT_ROOT.relative_to(_BOOTSTRAP_ROOT).as_posix()
        require_real_directory_beneath(_BOOTSTRAP_ROOT, output_relative)
        catalog_relative = _CATALOG_PATH.relative_to(_BOOTSTRAP_ROOT).as_posix()
        if read_regular_file_beneath(_BOOTSTRAP_ROOT, catalog_relative) != catalog_bytes:
            return False
    except (OSError, ValueError):
        return False
    expected_paths = {_BOOTSTRAP_ROOT / relative_path for relative_path in payloads}
    actual_paths = set(_OUTPUT_ROOT.glob("*.skill.json"))
    if actual_paths != expected_paths:
        return False
    try:
        return all(read_regular_file_beneath(_BOOTSTRAP_ROOT, relative_path) == content for relative_path, content in payloads.items())
    except ValueError:
        return False


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symbolic link output: {path}")


def _atomic_write(path: Path, content: bytes) -> None:
    _reject_symlink(path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write(catalog_bytes: bytes, payloads: dict[str, bytes]) -> None:
    try:
        output_relative = _OUTPUT_ROOT.relative_to(_BOOTSTRAP_ROOT).as_posix()
        catalog_relative = _CATALOG_PATH.relative_to(_BOOTSTRAP_ROOT).as_posix()
    except (OSError, ValueError):
        raise ValueError("refusing unsafe bootstrap output path") from None
    ensure_real_directory_beneath(_BOOTSTRAP_ROOT, output_relative)
    expected_paths = {_BOOTSTRAP_ROOT / relative_path for relative_path in payloads}
    existing_paths = tuple(_OUTPUT_ROOT.glob("*.skill.json"))
    for path in (*existing_paths, *expected_paths, _CATALOG_PATH):
        _reject_symlink(path)
    for stale_path in existing_paths:
        if stale_path not in expected_paths:
            stale_path.unlink()
    for relative_path, content in payloads.items():
        destination = _BOOTSTRAP_ROOT / relative_path
        _atomic_write(destination, content)
    _atomic_write(_BOOTSTRAP_ROOT / catalog_relative, catalog_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when generated archives or catalog entries are stale",
    )
    args = parser.parse_args()
    try:
        catalog_bytes, payloads = _expected_outputs()
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ProjectSkillImportError,
    ):
        print("error: public system Skill catalog generation failed", file=sys.stderr)
        return 1
    if args.check:
        if not _check(catalog_bytes, payloads):
            print("error: public system Skill catalog is stale", file=sys.stderr)
            return 1
        return 0
    _write(catalog_bytes, payloads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
