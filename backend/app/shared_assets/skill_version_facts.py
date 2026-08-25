from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SkillVersionArchiveFacts:
    file_count: int
    content_size_bytes: int
    payload_checksum: str


def skill_version_archive_facts(
    files: Sequence[tuple[str, str, int]],
) -> SkillVersionArchiveFacts:
    canonical_files = sorted(files, key=lambda item: item[0])
    canonical = json.dumps(
        [
            {
                "path": path,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
            for path, sha256, size_bytes in canonical_files
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return SkillVersionArchiveFacts(
        file_count=len(canonical_files),
        content_size_bytes=sum(size_bytes for _path, _sha256, size_bytes in canonical_files),
        payload_checksum=hashlib.sha256(canonical).hexdigest(),
    )
