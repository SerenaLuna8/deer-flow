"""Canonical package digest for review snapshots."""

from __future__ import annotations

import hashlib
from typing import Any

from deerflow.skills.review.models import normalize_relative_path


def compute_package_digest(snapshot: dict[str, Any]) -> str:
    """Return a host-path-independent SHA-256 digest."""
    records: list[bytes] = []
    for file_entry in snapshot.get("files", []):
        path = normalize_relative_path(str(file_entry["path"]))
        kind = str(file_entry.get("kind") or "unknown")
        size = int(file_entry.get("size") or 0)
        content_digest = str(file_entry.get("sha256") or "")
        records.append(
            b"\0".join(
                [
                    kind.encode(),
                    path.encode(),
                    str(size).encode("ascii"),
                    content_digest.encode("ascii"),
                ]
            )
        )

    digest = hashlib.sha256()
    for record in sorted(records):
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return f"sha256:{digest.hexdigest()}"
