"""Pure local document-extraction contracts and adapters.

This package deliberately has no host persistence or authorization dependency.
"""

from .base import BaseExtractor
from .contracts import (
    Attachment,
    AttachmentOccurrence,
    AttachmentSink,
    ChunkProfile,
    Document,
    ExtractionContext,
    ExtractionError,
    ExtractionLimits,
    ExtractionResult,
    ExtractSetting,
    HeaderRule,
    LocalAttachment,
    ParseProfile,
    ParseWarning,
    ProcessingProfile,
    SourceSpan,
)
from .manifest import canonical_parse_fingerprint, decode_manifest, encode_manifest

__all__ = [
    "Attachment",
    "AttachmentOccurrence",
    "AttachmentSink",
    "BaseExtractor",
    "ChunkProfile",
    "Document",
    "ExtractSetting",
    "ExtractionContext",
    "ExtractionError",
    "ExtractionLimits",
    "ExtractionResult",
    "HeaderRule",
    "LocalAttachment",
    "ParseProfile",
    "ParseWarning",
    "ProcessingProfile",
    "SourceSpan",
    "canonical_parse_fingerprint",
    "decode_manifest",
    "encode_manifest",
]
