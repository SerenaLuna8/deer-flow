"""Server-owned derived-object grammar, separate from original document keys."""

import re
from uuid import UUID

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _prefix(project_id: UUID, base_id: UUID, document_id: UUID, extraction_id: UUID) -> str:
    return f"projects/{project_id}/knowledge/{base_id}/{document_id}/extractions/{extraction_id}/"


def attachment_storage_key(project_id: UUID, base_id: UUID, document_id: UUID, extraction_id: UUID, sha256: str, media_type: str) -> str:
    if not _SHA.fullmatch(sha256) or media_type not in _EXTENSIONS:
        raise ValueError("invalid normalized attachment identity")
    return _prefix(project_id, base_id, document_id, extraction_id) + f"assets/{sha256}.{_EXTENSIONS[media_type]}"


def manifest_storage_key(project_id: UUID, base_id: UUID, document_id: UUID, extraction_id: UUID) -> str:
    return _prefix(project_id, base_id, document_id, extraction_id) + "manifest.json"


def is_extraction_storage_key(key: str, *, project_id: UUID, base_id: UUID, document_id: UUID, extraction_id: UUID) -> bool:
    prefix = _prefix(project_id, base_id, document_id, extraction_id)
    if not key.startswith(prefix):
        return False
    tail = key[len(prefix) :]
    return tail == "manifest.json" or re.fullmatch(r"assets/[0-9a-f]{64}\.(png|jpg|webp)", tail) is not None
