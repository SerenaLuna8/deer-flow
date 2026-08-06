#!/usr/bin/env python3
"""Run deterministic review for changed public Skill packages."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = REPO_ROOT / "backend" / "packages" / "harness"
if HARNESS_PATH.is_dir():
    sys.path.insert(0, str(HARNESS_PATH))

PUBLIC_SKILL_PACKAGE_PATHSPEC = ":(glob)skills/public/**"
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: PurePosixPath


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    diff_args = build_diff_args(args)
    result = _git_diff(repo_root, diff_args)
    if result.returncode != 0:
        fallback = build_force_push_fallback_diff_args(args)
        if fallback is None:
            sys.stderr.write("[skill-review] Failed to collect changed public Skills.\n")
            sys.stderr.write(result.stderr.decode(errors="replace"))
            return result.returncode
        diff_args = fallback
        result = _git_diff(repo_root, diff_args)
        if result.returncode != 0:
            sys.stderr.write("[skill-review] Failed to collect changed public Skills.\n")
            sys.stderr.write(result.stderr.decode(errors="replace"))
            return result.returncode

    packages = select_skill_packages(
        parse_name_status(result.stdout),
        repo_root,
    )
    if not packages:
        print("[skill-review] No changed public Skill package; skipping.")
        return 0

    failed = False
    for package in packages:
        if run_review(package, repo_root, args.python) != 0:
            failed = True
    if failed:
        print("[skill-review] One or more Skill reviews failed.")
        return 1
    print("[skill-review] All changed public Skills passed review.")
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review changed public Skill packages.")
    parser.add_argument("--base-ref", "--base_ref", dest="base_ref")
    parser.add_argument("--head-ref", "--head_ref", dest="head_ref")
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
    )
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)
    has_pr_args = bool(args.base_ref or args.head_ref)
    has_push_args = bool(args.before or args.after)
    if has_pr_args == has_push_args:
        parser.error("pass either --base-ref/--head-ref or --before/--after")
    if has_pr_args and not (args.base_ref and args.head_ref):
        parser.error("--base-ref and --head-ref must be provided together")
    if has_push_args and not (args.before and args.after):
        parser.error("--before and --after must be provided together")
    return args


def build_diff_args(args: argparse.Namespace) -> list[str]:
    if args.base_ref and args.head_ref:
        return [
            "--name-status",
            "-z",
            f"{args.base_ref}...{args.head_ref}",
        ]
    before = str(args.before)
    after = str(args.after)
    if is_zero_sha(before):
        return ["--name-status", "-z", EMPTY_TREE_SHA, after]
    return ["--name-status", "-z", before, after]


def build_force_push_fallback_diff_args(
    args: argparse.Namespace,
) -> list[str] | None:
    if not args.before or not args.after or is_zero_sha(str(args.before)):
        return None
    return [
        "--name-status",
        "-z",
        EMPTY_TREE_SHA,
        str(args.after),
    ]


def _git_diff(
    repo_root: Path,
    diff_args: list[str],
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "diff",
            *diff_args,
            "--",
            PUBLIC_SKILL_PACKAGE_PATHSPEC,
        ],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )


def parse_name_status(output: bytes) -> list[ChangedPath]:
    parts = [part for part in output.split(b"\0") if part]
    changes: list[ChangedPath] = []
    index = 0
    while index < len(parts):
        status = parts[index].decode(
            "utf-8",
            errors="surrogateescape",
        )
        index += 1
        if not status:
            continue
        path_index = index + 1 if status[0] in {"C", "R"} else index
        if path_index >= len(parts):
            raise ValueError(f"Malformed git diff --name-status near {status!r}")
        path = parts[path_index].decode(
            "utf-8",
            errors="surrogateescape",
        )
        changes.append(ChangedPath(status=status, path=PurePosixPath(path)))
        index = path_index + 1
    return changes


def select_skill_packages(
    changes: Sequence[ChangedPath],
    repo_root: Path,
) -> list[Path]:
    statuses: dict[PurePosixPath, list[str]] = {}
    order: list[PurePosixPath] = []
    for change in changes:
        if not is_public_skill_package_path(change.path):
            continue
        package = find_public_skill_package(change.path, repo_root)
        if package is None:
            continue
        if package not in statuses:
            statuses[package] = []
            order.append(package)
        statuses[package].append(change.status)

    packages: list[Path] = []
    for package in order:
        if is_fully_removed_package(
            package,
            statuses[package],
            repo_root,
        ):
            continue
        packages.append(repo_root / package)
    return packages


def is_fully_removed_package(
    package_rel: PurePosixPath,
    statuses: Sequence[str],
    repo_root: Path,
) -> bool:
    return bool(statuses) and all(status.startswith("D") for status in statuses) and not (repo_root / package_rel).is_dir()


def is_public_skill_package_path(path: PurePosixPath) -> bool:
    parts = path.parts
    return len(parts) >= 3 and parts[0] == "skills" and parts[1] == "public"


def find_public_skill_package(
    path: PurePosixPath,
    repo_root: Path,
) -> PurePosixPath | None:
    if not is_public_skill_package_path(path):
        return None
    current = path.parent
    while len(current.parts) >= 3:
        skill_md = current / "SKILL.md"
        if not _is_eval_fixture_skill_md(skill_md) and (repo_root / skill_md).is_file():
            return current
        if len(current.parts) == 3:
            return current
        current = current.parent
    return None


def _is_eval_fixture_skill_md(path: PurePosixPath) -> bool:
    from deerflow.skills.review.package_paths import (
        is_eval_fixture_skill_md,
    )

    return is_eval_fixture_skill_md(path)


def run_review(
    package: Path,
    repo_root: Path,
    python_executable: str,
) -> int:
    package_rel = package.relative_to(repo_root).as_posix()
    result = subprocess.run(
        [
            python_executable,
            "-m",
            "deerflow.skills.review.cli",
            package_rel,
            "--format",
            "text",
            "--fail-on",
            "error",
            "--fail-on-incomplete",
        ],
        cwd=repo_root,
        env=review_env(repo_root),
        check=False,
    )
    return result.returncode


def review_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    harness_path = repo_root / "backend" / "packages" / "harness"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(harness_path) if not existing else f"{harness_path}{os.pathsep}{existing}"
    return env


def is_zero_sha(value: str) -> bool:
    return len(value) in {40, 64} and set(value) == {"0"}


if __name__ == "__main__":
    sys.exit(main())
