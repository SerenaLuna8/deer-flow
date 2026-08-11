#!/usr/bin/env python3
# ruff: noqa: E402
"""Generate or verify the shared Workflow Node Registry v1 manifest."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_ROOT))

from deerflow.workflows.catalog_contracts import first_batch_node_registry_manifest_v1

_REPOSITORY_ROOT = _BACKEND_ROOT.parent
_FRONTEND_ROOT = _REPOSITORY_ROOT / "frontend"
_OUTPUT_PATH = _REPOSITORY_ROOT / "frontend/src/core/project-workflows/node-registry-v1.json"
_PRETTIER_PATH = _FRONTEND_ROOT / "node_modules/.bin/prettier"


def _expected_manifest() -> list[dict[str, object]]:
    return first_batch_node_registry_manifest_v1()


def _expected_content() -> bytes:
    raw_content = json.dumps(_expected_manifest(), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    completed = subprocess.run(
        [
            str(_PRETTIER_PATH),
            "--parser",
            "json",
            "--stdin-filepath",
            str(_OUTPUT_PATH),
        ],
        cwd=_FRONTEND_ROOT,
        input=raw_content,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise OSError("repository Prettier failed to format Workflow Node Registry v1")
    return completed.stdout


def _check() -> bool:
    if _OUTPUT_PATH.is_symlink():
        return False
    try:
        actual_content = _OUTPUT_PATH.read_bytes()
        actual_manifest = json.loads(actual_content)
        expected_manifest = _expected_manifest()
        expected_content = _expected_content()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return actual_manifest == expected_manifest and actual_content == expected_content


def _write() -> None:
    if _OUTPUT_PATH.is_symlink():
        raise ValueError(f"refusing symbolic link output: {_OUTPUT_PATH}")
    content = _expected_content()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=_OUTPUT_PATH.parent,
        prefix=f".{_OUTPUT_PATH.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, _OUTPUT_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in manifest is stale or not repository-formatted",
    )
    args = parser.parse_args()
    try:
        if args.check:
            if not _check():
                print("error: Workflow Node Registry v1 manifest is stale", file=sys.stderr)
                return 1
            return 0
        _write()
    except (OSError, TypeError, ValueError):
        print("error: Workflow Node Registry v1 generation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
