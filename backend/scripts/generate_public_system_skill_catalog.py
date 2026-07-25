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
from pathlib import Path

from app.shared_assets.bootstrap.skill_archive import dump_skill_archive
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.skill_service import _analyze_skill_files
from scripts.import_project_skills import load_project_skill_sources

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPOSITORY_ROOT / "skills" / "public"
_BOOTSTRAP_ROOT = _REPOSITORY_ROOT / "backend" / "app" / "shared_assets" / "bootstrap"
_CATALOG_PATH = _BOOTSTRAP_ROOT / "catalog.json"
_OUTPUT_ROOT = _BOOTSTRAP_ROOT / "content" / "public-skills"
_PAYLOAD_PREFIX = "content/public-skills/"
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_EXPECTED_SKILL_COUNT = 21


def _skill_name(files) -> str:
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
    return name


def _expected_outputs() -> tuple[bytes, dict[str, bytes]]:
    raw_catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    retained_entries = [entry for entry in raw_catalog["entries"] if not str(entry.get("payload_path", "")).startswith(_PAYLOAD_PREFIX)]
    generated_entries: list[dict[str, object]] = []
    payloads: dict[str, bytes] = {}
    seen_names: set[str] = set()

    sources = load_project_skill_sources(_SOURCE_ROOT)
    if len(sources) != _EXPECTED_SKILL_COUNT:
        raise ValueError("public Skill catalog count is invalid")
    for source in sources:
        name = _skill_name(source.files)
        if name in seen_names:
            raise ValueError("public Skill names must be unique")
        seen_names.add(name)
        relative_path = f"{_PAYLOAD_PREFIX}{name}-v1.skill.json"
        archive = dump_skill_archive(source.files)
        payloads[relative_path] = archive
        generated_entries.append(
            {
                "source_key": f"builtin:skill:{name}",
                "kind": "skill",
                "slug": name,
                "display_name": name,
                "version": 1,
                "payload_path": relative_path,
                "payload_format": "skill_archive_v1",
                "sha256": hashlib.sha256(archive).hexdigest(),
            }
        )

    kind_order = {"skill": 0, "mcp": 1, "agent": 2}
    entries = sorted(
        [*retained_entries, *generated_entries],
        key=lambda entry: (
            kind_order[str(entry["kind"])],
            str(entry["source_key"]),
        ),
    )
    catalog = {
        "schema_version": raw_catalog["schema_version"],
        "entries": entries,
    }
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
    if _OUTPUT_ROOT.is_symlink() or _CATALOG_PATH.is_symlink() or _CATALOG_PATH.read_bytes() != catalog_bytes:
        return False
    expected_paths = {_BOOTSTRAP_ROOT / relative_path for relative_path in payloads}
    actual_paths = set(_OUTPUT_ROOT.glob("*.skill.json"))
    if actual_paths != expected_paths:
        return False
    return all(not (_BOOTSTRAP_ROOT / relative_path).is_symlink() and (_BOOTSTRAP_ROOT / relative_path).read_bytes() == content for relative_path, content in payloads.items())


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
    _reject_symlink(_OUTPUT_ROOT)
    _OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
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
    _atomic_write(_CATALOG_PATH, catalog_bytes)


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
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
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
