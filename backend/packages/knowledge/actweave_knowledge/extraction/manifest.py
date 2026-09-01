"""Canonical extraction-manifest serialization and resource validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from .contracts import ExtractionError, ExtractionLimits, ExtractionResult, ParseProfile
from .markdown_images import MarkdownImagePositionError, find_markdown_images, markdown_references

_LOGICAL_ATTACHMENT_PREFIX = "knowledge-attachment:"
_LOGICAL_IMAGE = re.compile(r"!\[(?:\\.|[^\]\\])*\]\(knowledge-attachment:([0-9a-f]{64})\)")


def canonical_parse_fingerprint(profile: ParseProfile) -> str:
    """Hash only parse-affecting inputs; chunking has a separate cache boundary."""

    payload = json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quota_error() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")


def _validate_document_logical_images(result: ExtractionResult, asset_refs: set[str]) -> None:
    rendered_refs: set[str] = set()
    references = markdown_references("\n\n".join(document.page_content for document in result.documents))
    for document in result.documents:
        candidates = []
        try:
            images = find_markdown_images(document.page_content, references=references)
        except MarkdownImagePositionError:
            raise ExtractionError("INVALID_MANIFEST") from None
        for image in images:
            if not image.source.startswith(_LOGICAL_ATTACHMENT_PREFIX):
                continue
            canonical = _LOGICAL_IMAGE.fullmatch(document.page_content[image.start : image.end])
            if canonical is None or image.source != _LOGICAL_ATTACHMENT_PREFIX + canonical.group(1):
                raise ExtractionError("INVALID_MANIFEST")
            candidates.append((canonical.group(1), image.start, image.end))
        links = Counter(candidates)
        occurrences = Counter((occurrence.ref, occurrence.source.start, occurrence.source.end) for occurrence in document.attachments)
        if links != occurrences:
            raise ExtractionError("INVALID_MANIFEST")
        rendered_refs.update(ref for ref, _, _ in candidates)
    if rendered_refs != asset_refs:
        raise ExtractionError("INVALID_MANIFEST")


def validate_result(result: ExtractionResult, limits: ExtractionLimits) -> None:
    """Apply the supplied current limits and validate asset/occurrence closure."""

    assets = {asset.ref: asset for asset in result.attachments}
    if len(assets) != len(result.attachments):
        raise ExtractionError("INVALID_MANIFEST")
    if (
        sum(len(document.page_content) for document in result.documents) > limits.max_text_chars
        or len(assets) > limits.max_images
        or sum(asset.size_bytes for asset in assets.values()) > limits.max_total_image_bytes
        or any(asset.size_bytes > limits.max_image_bytes or asset.width * asset.height > limits.max_image_pixels for asset in assets.values())
    ):
        raise _quota_error()

    _validate_document_logical_images(result, set(assets))


def encode_manifest(result: ExtractionResult) -> bytes:
    """Encode a deterministic, path-free format-v1 manifest."""

    limits = ExtractionLimits()
    validate_result(result, limits)
    payload = json.dumps(
        {"format_version": 1, "result": result.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > limits.max_manifest_bytes:
        raise _quota_error()
    return payload


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def decode_manifest(payload: bytes, limits: ExtractionLimits) -> ExtractionResult:
    """Decode a complete v1 manifest or reject it without returning a partial result."""

    if len(payload) > limits.max_manifest_bytes:
        raise _quota_error()
    try:
        envelope = json.loads(payload, parse_constant=_reject_json_constant)
        if not isinstance(envelope, dict) or set(envelope) != {"format_version", "result"}:
            raise ValueError("manifest format")
    except (TypeError, UnicodeDecodeError, ValueError):
        raise ExtractionError("INVALID_MANIFEST") from None

    format_version = envelope["format_version"]
    if not isinstance(format_version, int) or isinstance(format_version, bool):
        raise ExtractionError("INVALID_MANIFEST")
    if format_version != 1:
        raise ExtractionError("PARSER_PROFILE_UNAVAILABLE")
    try:
        result = ExtractionResult.model_validate(envelope["result"])
    except (TypeError, ValueError):
        raise ExtractionError("INVALID_MANIFEST") from None
    validate_result(result, limits)
    return result
