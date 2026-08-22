"""Byte-preserving patches for managed ``SKILL.md`` frontmatter fields."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .frontmatter import (
    ManagedSkillFrontmatterField,
    SkillFrontmatterPatchRejected,
    SkillFrontmatterPatchResult,
    SkillFrontmatterSourceMismatch,
    SkillSecretProjection,
    _diagnostic,
    _parse_skill_frontmatter_document,
    _validate_requested_projection,
    skill_frontmatter_source_sha256,
)
from .types import SecretRequirement


def _render_required_secrets(
    requirements: Sequence[SecretRequirement],
    newline: str,
) -> str:
    lines = ["required-secrets:"]
    for requirement in requirements:
        # The POSIX-name grammar excludes quote and escape characters. Always
        # quoting still matters for YAML 1.1 words such as YES, NO, ON, and NULL.
        lines.extend(
            (
                f'  - name: "{requirement.name}"',
                f'    target_env: "{requirement.target_env}"',
                f"    optional: {'true' if requirement.optional else 'false'}",
            )
        )
    return newline.join(lines)


def _render_secrets_autonomous(value: bool) -> str:
    return f"secrets-autonomous: {'true' if value else 'false'}"


def _remove_existing_field(
    content: str,
    *,
    parsed: Any,
    field_name: ManagedSkillFrontmatterField,
) -> str:
    envelope = parsed.envelope
    location = parsed.managed_fields[field_name]
    assert envelope is not None
    start = envelope.yaml_start + location.start
    end = envelope.yaml_start + location.end

    # A final field has no newline inside yaml_text because the extractor owns
    # the separator immediately before the closing fence. Include its preceding
    # newline so removal does not introduce a blank line.
    if location.end == len(envelope.yaml_text) and location.start > 0:
        newline = envelope.newline
        preceding = start - len(newline)
        if preceding >= envelope.yaml_start and content[preceding:start] == newline:
            start = preceding
    return content[:start] + content[end:]


def _replace_existing_field(
    content: str,
    *,
    parsed: Any,
    field_name: ManagedSkillFrontmatterField,
    replacement: str,
) -> str:
    envelope = parsed.envelope
    location = parsed.managed_fields[field_name]
    assert envelope is not None
    start = envelope.yaml_start + location.start
    end = envelope.yaml_start + location.end
    suffix = envelope.newline if location.end < len(envelope.yaml_text) else ""
    return content[:start] + replacement + suffix + content[end:]


def _insert_field(
    content: str,
    *,
    parsed: Any,
    field_name: ManagedSkillFrontmatterField,
    rendered: str,
) -> str:
    envelope = parsed.envelope
    assert envelope is not None

    # Keep required-secrets before an existing autonomous flag for predictable
    # managed-field order. Otherwise append immediately before the closing fence
    # without touching existing frontmatter bytes.
    if field_name == "required-secrets" and "secrets-autonomous" in parsed.managed_fields:
        location = parsed.managed_fields["secrets-autonomous"]
        insertion = envelope.yaml_start + location.start
        return content[:insertion] + rendered + envelope.newline + content[insertion:]

    insertion = envelope.yaml_end
    separator = "" if not envelope.yaml_text or envelope.yaml_text.endswith(envelope.newline) else envelope.newline
    return content[:insertion] + separator + rendered + content[insertion:]


def _patch_one_field(
    content: str,
    *,
    field_name: ManagedSkillFrontmatterField,
    replacement: str | None,
) -> str:
    parsed = _parse_skill_frontmatter_document(content)
    if not parsed.result.valid or not parsed.result.patchable:
        raise SkillFrontmatterPatchRejected(
            parsed.result.source_sha256,
            parsed.result.diagnostics,
        )
    if field_name in parsed.managed_fields:
        if replacement is None:
            return _remove_existing_field(
                content,
                parsed=parsed,
                field_name=field_name,
            )
        return _replace_existing_field(
            content,
            parsed=parsed,
            field_name=field_name,
            replacement=replacement,
        )
    if replacement is None:
        return content
    return _insert_field(
        content,
        parsed=parsed,
        field_name=field_name,
        rendered=replacement,
    )


def patch_skill_frontmatter_document(
    content: str,
    *,
    expected_source_sha256: str | None,
    required_secrets: Sequence[SecretRequirement],
    secrets_autonomous: bool,
) -> SkillFrontmatterPatchResult:
    """Patch only required-secrets and secrets-autonomous in full SKILL.md.

    The expected checksum is optional for non-HTTP internal callers. When it is
    supplied, a mismatch is rejected before parsing or changing the source.
    """

    source_sha256 = skill_frontmatter_source_sha256(content)
    if expected_source_sha256 is not None and expected_source_sha256 != source_sha256:
        raise SkillFrontmatterSourceMismatch(
            expected_source_sha256,
            source_sha256,
        )

    requested, request_diagnostics = _validate_requested_projection(
        required_secrets,
        secrets_autonomous,
    )
    if requested is None:
        raise SkillFrontmatterPatchRejected(
            source_sha256,
            request_diagnostics,
        )

    parsed = _parse_skill_frontmatter_document(content)
    if not parsed.result.valid or not parsed.result.patchable:
        raise SkillFrontmatterPatchRejected(
            source_sha256,
            parsed.result.diagnostics,
        )
    current = parsed.result.projection
    assert current is not None

    changed_fields: list[ManagedSkillFrontmatterField] = []
    patched_content = content

    if current.required_secrets != requested.required_secrets:
        changed_fields.append("required-secrets")
        required_replacement = (
            _render_required_secrets(
                requested.required_secrets,
                parsed.envelope.newline,
            )
            if requested.required_secrets
            else None
        )
        patched_content = _patch_one_field(
            patched_content,
            field_name="required-secrets",
            replacement=required_replacement,
        )

    if current.secrets_autonomous != requested.secrets_autonomous:
        changed_fields.append("secrets-autonomous")
        reparsed = _parse_skill_frontmatter_document(patched_content)
        assert reparsed.envelope is not None
        patched_content = _patch_one_field(
            patched_content,
            field_name="secrets-autonomous",
            replacement=_render_secrets_autonomous(requested.secrets_autonomous),
        )

    final = _parse_skill_frontmatter_document(patched_content)
    if (
        not final.result.valid
        or not final.result.patchable
        or final.result.projection is None
        or final.result.projection.required_secrets != requested.required_secrets
        or final.result.projection.secrets_autonomous != requested.secrets_autonomous
    ):
        raise SkillFrontmatterPatchRejected(
            source_sha256,
            (_diagnostic("patch_verification_failed"),),
        )

    result_sha256 = final.result.source_sha256
    projection = SkillSecretProjection(
        required_secrets=final.result.projection.required_secrets,
        secrets_autonomous=final.result.projection.secrets_autonomous,
        secrets_autonomous_explicit=(final.result.projection.secrets_autonomous_explicit),
        shorthand_count=final.result.projection.shorthand_count,
    )
    return SkillFrontmatterPatchResult(
        source_sha256=source_sha256,
        result_sha256=result_sha256,
        content=patched_content,
        changed=patched_content != content,
        changed_fields=tuple(changed_fields),
        projection=projection,
        diagnostics=final.result.diagnostics,
    )


__all__ = ["patch_skill_frontmatter_document"]
