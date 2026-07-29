"""Deterministic package resource graph checks."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from deerflow.skills.review.models import (
    make_finding,
    normalize_relative_path,
)
from deerflow.skills.review.package_paths import is_eval_fixture_path

_MARKDOWN_LINK_RE = re.compile(r'!?\[[^\]]*]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./-])(?:references|scripts|templates|assets|evals)"
    r"/[A-Za-z0-9._~/%+-]+"
)
_RESOURCE_DIRS = {
    "references",
    "scripts",
    "templates",
    "assets",
    "evals",
}


def build_resource_graph(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    files = {str(entry["path"]): entry for entry in snapshot.get("files", [])}
    nodes = [{"path": path, "kind": files[path].get("kind", "unknown")} for path in sorted(files)]
    edges: set[tuple[str, str]] = set()
    missing: set[tuple[str, str]] = set()
    escaping: set[tuple[str, str]] = set()

    for path, entry in files.items():
        if is_eval_fixture_path(path) or entry.get("kind") != "text":
            continue
        for raw_ref in _extract_references(str(entry.get("content") or "")):
            resolved = _resolve_reference(path, raw_ref)
            if resolved is None:
                continue
            if resolved == "__ESCAPES__":
                escaping.add((path, raw_ref))
            elif resolved in files:
                edges.add((path, resolved))
            else:
                missing.add((path, resolved))

    referenced = {target for _, target in edges}
    resource_paths = {path for path in files if PurePosixPath(path).parts and PurePosixPath(path).parts[0] in _RESOURCE_DIRS}
    orphans = sorted(resource_paths - referenced - {"evals/evals.json", "evals/trigger_eval_set.json"})
    orphans = [path for path in orphans if not is_eval_fixture_path(path)]

    findings: list[dict[str, Any]] = []
    for source, target in sorted(missing):
        findings.append(
            make_finding(
                "resource.missing",
                severity="warning",
                path=source,
                message=f"Referenced resource does not exist: {target}",
                remediation=("Add the referenced file, correct the path, or remove the stale reference."),
                evidence=target,
            )
        )
    for source, raw_ref in sorted(escaping):
        findings.append(
            make_finding(
                "resource.escaping-link",
                severity="warning",
                path=source,
                message=(f"Reference escapes the package boundary: {raw_ref}"),
                remediation=("Keep Skill references package-relative and inside the Skill directory."),
                evidence=raw_ref,
            )
        )
    for orphan in orphans:
        findings.append(
            make_finding(
                "resource.unreferenced",
                severity="warning",
                path=orphan,
                message="Resource is not reachable from SKILL.md.",
                remediation=("Reference the file with read-when guidance or remove it."),
            )
        )

    return (
        {
            "nodes": nodes,
            "edges": [{"source": source, "target": target} for source, target in sorted(edges)],
            "orphans": orphans,
        },
        findings,
    )


def _extract_references(content: str) -> set[str]:
    refs = {match.group(1).split("#", 1)[0] for match in _MARKDOWN_LINK_RE.finditer(content)}
    for match in _CODE_SPAN_RE.finditer(content):
        token = match.group(1).strip()
        if "/" in token:
            refs.add(token)
    refs.update(match.group(0) for match in _PATH_TOKEN_RE.finditer(content))
    return refs


def _resolve_reference(source_path: str, raw_ref: str) -> str | None:
    ref = raw_ref.strip().strip("\"'")
    if not ref or ref.startswith("#") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", ref):
        return None
    try:
        if ref.startswith("/"):
            return "__ESCAPES__"
        if "://" in ref:
            return None
        candidate = (PurePosixPath(source_path).parent / ref).as_posix()
        return normalize_relative_path(candidate)
    except ValueError:
        return "__ESCAPES__"
