"""Server-owned processing identities; user inputs cannot choose runtime versions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..asyncio_utils import run_sync_to_completion
from ..contracts import KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR, KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR, KnowledgeSettings
from ..extraction.contracts import ChunkProfile, ExtractionError, ExtractionLimits, ExtractSetting, HeaderRule, LocalAttachment, ParseProfile, ProcessingProfile
from ..extraction.registry import ExtractorRegistry, default_registry
from ..extraction.runtime import run_extraction
from .tokenizer import TOKENIZER_PROFILE_ID, count_knowledge_tokens, tokenizer_fingerprint

NORMALIZATION_VERSION = "md-v1"
IMAGE_POLICY_VERSION = "raster-v1"
CLEANER_VERSION = "cleaner-v1"
SPLITTER_VERSION = "splitter-v1"


class ProcessingParameters(BaseModel):
    """Only user-configurable fields, independent of parser and resource identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    unit: Literal["character", "token"] = "token"
    mode: Literal["general", "parent_child"] = "general"
    size: int = Field(default=1000, ge=200, le=4000, strict=True)
    overlap: int = Field(default=100, ge=0, le=500, strict=True)
    separator: str = Field(default=KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR, min_length=1, max_length=64, strict=True)
    child_size: int = Field(default=500, ge=100, le=2000, strict=True)
    child_separator: str = Field(default=KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR, min_length=1, max_length=64, strict=True)
    remove_extra_spaces: bool = Field(default=False, strict=True)
    remove_urls_emails: bool = Field(default=False, strict=True)
    header_rules: tuple[HeaderRule, ...] = ()

    @model_validator(mode="after")
    def validate_relationships(self) -> ProcessingParameters:
        if self.overlap >= self.size or (self.mode == "parent_child" and self.child_size >= self.size):
            raise ValueError("chunk overlap and child size must be smaller than parent")
        if len({item.sheet for item in self.header_rules}) != len(self.header_rules):
            raise ValueError("duplicate sheet header rule")
        return self


def _extension(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\.[A-Za-z0-9]+", value) is None:
        raise ValueError("invalid file extension")
    return value.lower()


def preview_fingerprint(*, source_sha256: str, extension: str, profile: ProcessingProfile, capability_revision: str) -> str:
    if not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("invalid source digest")
    if not isinstance(capability_revision, str) or not capability_revision:
        raise ValueError("missing capability revision")
    payload = {"source_sha256": source_sha256, "extension": _extension(extension), "profile": profile.model_dump(mode="json"), "capability_revision": capability_revision}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def resolve_processing_profile(settings: KnowledgeSettings, user_parameters: ProcessingParameters, registry: ExtractorRegistry, *, extension: str) -> ProcessingProfile:
    parameters = ProcessingParameters.model_validate(user_parameters)
    extension = _extension(extension)
    registration = registry.resolve(datasource_type="file", etl_type=settings.etl_type, extension=extension)
    reason = registration.dependency_probe()
    if reason:
        raise ExtractionError(reason)
    if parameters.header_rules and extension not in {".csv", ".xls", ".xlsx"}:
        raise ValueError("header rules require a table format")
    if extension == ".csv" and any(item.sheet is not None for item in parameters.header_rules):
        raise ValueError("CSV header rule has no sheet")
    if parameters.unit == "token":
        count_knowledge_tokens("ready")  # verifies the actual bundled vocabulary
    return ProcessingProfile(
        parse=ParseProfile(
            etl_type=settings.etl_type,
            extractor_id=registration.extractor_id,
            extractor_version=registration.extractor_version,
            normalization_version=NORMALIZATION_VERSION,
            image_policy_version=IMAGE_POLICY_VERSION,
            header_rules=parameters.header_rules,
        ),
        chunk=ChunkProfile(
            **parameters.model_dump(exclude={"header_rules"}),
            tokenizer_profile_id=TOKENIZER_PROFILE_ID if parameters.unit == "token" else None,
            tokenizer_digest=tokenizer_fingerprint() if parameters.unit == "token" else None,
            cleaner_version=CLEANER_VERSION,
            splitter_version=SPLITTER_VERSION,
        ),
    )


class FileFormatCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    extension: str
    parser_id: str
    available: bool
    reason_code: str | None
    embedded_images: bool


class FileChunkLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    unit: Literal["token"] = "token"
    tokenizer_profile_id: str = TOKENIZER_PROFILE_ID
    parent_min: int = 200
    parent_max: int = 4000
    parent_max_chars: int = 4000
    overlap_max: int = 500
    child_min: int = 100
    child_max: int = 2000


class FileCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    effective_etl: Literal["dify", "unstructured_local"]
    capability_revision: str
    formats: tuple[FileFormatCapability, ...]
    chunk_limits: FileChunkLimits = Field(default_factory=FileChunkLimits)


def build_file_capabilities(settings: KnowledgeSettings, registry: ExtractorRegistry, *, runtime_reason: str | None = None) -> FileCapabilities:
    """Build once for this process; request reads never probe or spawn parsers."""
    tokenizer_digest = None
    try:
        count_knowledge_tokens("ready")
        tokenizer_digest = tokenizer_fingerprint()
    except ExtractionError:
        runtime_reason = runtime_reason or "TOKENIZER_UNAVAILABLE"
    formats = []
    versions = []
    for registration in registry.registrations:
        if settings.etl_type not in registration.etl_types:
            continue
        reason = runtime_reason or registration.dependency_probe()
        for extension in registration.extensions:
            formats.append(FileFormatCapability(extension=extension, parser_id=registration.extractor_id, available=reason is None, reason_code=reason, embedded_images=registration.supports_embedded_images))
            versions.append((extension, registration.extractor_id, registration.extractor_version))
    formats.sort(key=lambda item: item.extension)
    payload = {
        "etl": settings.etl_type,
        "versions": sorted(versions),
        "formats": [item.model_dump() for item in formats],
        "tokenizer": tokenizer_digest,
        "normalizer": NORMALIZATION_VERSION,
        "images": IMAGE_POLICY_VERSION,
        "cleaner": CLEANER_VERSION,
        "splitter": SPLITTER_VERSION,
    }
    revision = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return FileCapabilities(effective_etl=settings.etl_type, capability_revision=revision, formats=tuple(formats))


def required_file_formats_ready(capabilities: FileCapabilities) -> bool:
    required = {".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".html", ".pptx", ".epub"}
    return required <= {item.extension for item in capabilities.formats if item.available}


async def probe_file_capabilities(settings: KnowledgeSettings) -> FileCapabilities:
    """One bounded, fixed, temp-only parser run at startup or admin enable."""
    registry = await run_sync_to_completion(default_registry)
    reason = None
    work_dir = None
    try:
        work_dir = Path(await run_sync_to_completion(tempfile.mkdtemp, prefix="knowledge-readiness-"))
        source = work_dir / "probe.txt"
        await run_sync_to_completion(source.write_text, "Knowledge parser readiness.", encoding="utf-8")
        profile = await run_sync_to_completion(resolve_processing_profile, settings, ProcessingParameters(), registry, extension=".txt")

        async def guard() -> None:
            pass

        async def on_asset(asset: LocalAttachment) -> None:
            raise ExtractionError("PARSER_OUTPUT_INVALID")

        result = await run_extraction(
            ExtractSetting(source_path=source, original_name="probe.txt", datasource_type="file", profile=profile.parse), work_dir=work_dir, limits=ExtractionLimits(), timeout_seconds=15, on_asset=on_asset, guard=guard
        )
        if not result.documents or result.documents[0].page_content.strip() != "Knowledge parser readiness.":
            reason = "PARSER_OUTPUT_INVALID"
    except ExtractionError as error:
        reason = error.reason_code
    except (OSError, ValueError, TimeoutError):
        reason = "PARSER_SANDBOX_UNAVAILABLE"
    finally:
        if work_dir is not None:
            await run_sync_to_completion(shutil.rmtree, work_dir, ignore_errors=True)
    return await run_sync_to_completion(build_file_capabilities, settings, registry, runtime_reason=reason)


def validate_frozen_processing_profile(value: dict | None, *, extension: str, registry: ExtractorRegistry) -> ProcessingProfile:
    """Retry never substitutes the current ETL or fabricates a historical parser."""
    try:
        profile = ProcessingProfile.model_validate(value)
        parameters = ProcessingParameters(**profile.chunk.model_dump(exclude={"tokenizer_profile_id", "tokenizer_digest", "cleaner_version", "splitter_version"}), header_rules=profile.parse.header_rules)
        expected = resolve_processing_profile(KnowledgeSettings(etl_type=profile.parse.etl_type), parameters, registry, extension=extension)
        if expected != profile:
            raise ValueError("unsupported frozen processing version")
        return profile
    except (ValueError, ExtractionError):
        raise ExtractionError("PROCESSING_PROFILE_UNAVAILABLE", "原解析配置已不可用，请显式重新解析") from None


def chunk_settings(profile: ProcessingProfile) -> dict[str, object]:
    """Compatibility columns are projections of the exact frozen chunk profile."""
    chunk = profile.chunk
    return dict(
        chunk_size=chunk.size,
        chunk_overlap=chunk.overlap,
        chunk_separator=chunk.separator,
        chunking_mode=chunk.mode,
        child_chunk_size=chunk.child_size,
        child_chunk_separator=chunk.child_separator,
        remove_extra_spaces=chunk.remove_extra_spaces,
        remove_urls_emails=chunk.remove_urls_emails,
    )
