#!/usr/bin/env python3
"""Explicitly copy a legacy local runtime home into ``.act-weave``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HARNESS_ROOT = REPOSITORY_ROOT / "backend" / "packages" / "harness"
if HARNESS_ROOT.is_dir():
    sys.path.insert(0, str(HARNESS_ROOT))

from deerflow.config.runtime_home_migration import (  # noqa: E402
    RuntimeHomeMigrationError,
    RuntimeHomeMigrationResult,
    migrate_runtime_home,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全预演或复制旧的本地运行目录到 ActWeave .act-weave",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="仓库根目录（默认由脚本位置推导）",
    )
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        help="旧运行目录；可重复指定，省略时合并已存在的 .deer-flow 与 backend/.deer-flow",
    )
    parser.add_argument(
        "--target",
        type=Path,
        help="新运行目录（默认 <repository-root>/.act-weave）",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="执行复制；省略时只做 dry-run，不创建或修改目录",
    )
    return parser


def print_result(result: RuntimeHomeMigrationResult) -> None:
    mode = "copy" if result.applied else "dry-run"
    print(f"ActWeave runtime home migration: {mode}")
    for source in result.sources:
        print(f"source: {source}")
    print(f"target: {result.target}")
    print(f"files: {result.file_count}")
    print(f"bytes: {result.total_bytes}")
    print(f"verified: {'yes' if result.verified else 'no'}")
    print("sources retained: yes")
    if not result.applied:
        print("No files were copied. Review the paths, then rerun with --copy.")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = migrate_runtime_home(
            args.repository_root,
            source=args.source,
            target=args.target,
            apply=args.copy,
        )
    except (RuntimeHomeMigrationError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
