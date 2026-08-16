"""Canonical P1 contracts for Vision Bridge invocation and evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from deerflow.vision.prompt import VisionMode

MAX_EVIDENCE_JSON_BYTES = 24_000
MAX_SUMMARY_CHARS = 2_000
MAX_EVIDENCE_TEXT_CHARS = 2_000
MAX_LOCATION_CHARS = 256
MAX_OCR_TEXT_CHARS = 12_000
MAX_UNCERTAINTY_CHARS = 1_000

BoundedSummary = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_SUMMARY_CHARS),
]
BoundedEvidenceText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_EVIDENCE_TEXT_CHARS),
]
BoundedLocation = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_LOCATION_CHARS),
]
BoundedOcrText = Annotated[
    str,
    StringConstraints(max_length=MAX_OCR_TEXT_CHARS),
]
BoundedUncertainty = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_UNCERTAINTY_CHARS),
]


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class InspectImageInput(_StrictContract):
    image_path: str = Field(min_length=1, max_length=1_024)
    mode: VisionMode = "auto"


class VisionErrorResult(_StrictContract):
    ok: Literal[False]
    code: Literal[
        "IMAGE_UNAVAILABLE",
        "UNSUPPORTED_MEDIA",
        "IMAGE_TOO_LARGE",
        "IMAGE_PIXEL_LIMIT_EXCEEDED",
        "DATA_POLICY_BLOCKED",
        "VISION_BUSY",
        "VISION_RATE_LIMITED",
        "VISION_DEADLINE_EXCEEDED",
        "VISION_UNAVAILABLE",
        "VISION_AUTH_FAILED",
        "VISION_CONFIGURATION_ERROR",
        "VISION_CONTENT_BLOCKED",
        "VISION_RESPONSE_TOO_LARGE",
        "VISION_SCHEMA_MISMATCH",
    ]
    message: str = Field(min_length=1, max_length=200)


class VisionEvidenceItem(_StrictContract):
    kind: Literal["text", "layout", "visual", "chart", "table", "ui"]
    text: BoundedEvidenceText
    location: BoundedLocation

    @field_validator("text", "location")
    @classmethod
    def visible_fields_must_have_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence text and location must be non-empty")
        return value


class VisionOcrEvidence(_StrictContract):
    full_text: BoundedOcrText
    truncated: bool


class VisionEvidence(_StrictContract):
    ok: Literal[True]
    content_type: Literal["untrusted_image_evidence"]
    schema_version: Literal["vision.evidence.v1"]
    summary: BoundedSummary
    evidence: list[VisionEvidenceItem] = Field(max_length=64)
    ocr: VisionOcrEvidence | None = None
    uncertainty: list[BoundedUncertainty] = Field(max_length=32)
    partial: bool

    @field_validator("summary")
    @classmethod
    def summary_must_have_visible_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must be non-empty")
        return value

    @field_validator("uncertainty")
    @classmethod
    def uncertainty_must_have_visible_text(
        cls,
        value: list[str],
    ) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("uncertainty items must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_partial_evidence(self) -> VisionEvidence:
        if not self.evidence and (not self.partial or not self.uncertainty):
            raise ValueError(
                "empty evidence requires partial=true and uncertainty",
            )
        if self.partial and not self.uncertainty and not (self.ocr is not None and self.ocr.truncated):
            raise ValueError(
                "partial evidence requires uncertainty or OCR truncation",
            )
        return self

    def canonical_json(self) -> str:
        """Return bounded canonical JSON for the untrusted ToolMessage."""

        encoded = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > MAX_EVIDENCE_JSON_BYTES:
            raise ValueError("VISION_RESPONSE_TOO_LARGE")
        return encoded.decode("utf-8")


@dataclass(frozen=True, slots=True)
class VisionUsageReceipt:
    call_count: int
    request_dispatched: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_unknown: bool = True


@dataclass(frozen=True, slots=True)
class VisionInvocationResult:
    evidence: VisionEvidence
    usage_receipt: VisionUsageReceipt


__all__ = [
    "InspectImageInput",
    "MAX_EVIDENCE_TEXT_CHARS",
    "MAX_EVIDENCE_JSON_BYTES",
    "MAX_LOCATION_CHARS",
    "MAX_OCR_TEXT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_UNCERTAINTY_CHARS",
    "VisionEvidence",
    "VisionErrorResult",
    "VisionEvidenceItem",
    "VisionInvocationResult",
    "VisionMode",
    "VisionOcrEvidence",
    "VisionUsageReceipt",
]
