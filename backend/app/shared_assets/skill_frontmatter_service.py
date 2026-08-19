from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from functools import partial

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetForbidden,
    AssetValidationFailed,
    SkillFrontmatterSourceStale,
    SkillSecretDeclarationInvalid,
)
from deerflow.skills.frontmatter import (
    SkillFrontmatterParseResult,
    parse_skill_frontmatter_document,
)
from deerflow.skills.frontmatter_patch import (
    SkillFrontmatterPatchRejected,
    SkillFrontmatterPatchResult,
    SkillFrontmatterSourceMismatch,
    patch_skill_frontmatter_document,
)
from deerflow.skills.types import SecretRequirement

MAX_SKILL_FRONTMATTER_DOCUMENT_BYTES = 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class SkillFrontmatterService:
    """Stateless, project-authorized SKILL.md parse and patch boundary."""

    async def parse(
        self,
        actor: ProjectContext,
        content: str,
        *,
        expected_source_sha256: str,
    ) -> SkillFrontmatterParseResult:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)
        self._validate_input(actor, content, expected_source_sha256)
        result = await asyncio.to_thread(
            parse_skill_frontmatter_document,
            content,
        )
        if result.source_sha256 != expected_source_sha256:
            raise SkillFrontmatterSourceStale(actor.request_id)
        return result

    async def patch(
        self,
        actor: ProjectContext,
        content: str,
        *,
        expected_source_sha256: str,
        required_secrets: Sequence[SecretRequirement],
        secrets_autonomous: bool,
    ) -> SkillFrontmatterPatchResult:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        self._validate_input(actor, content, expected_source_sha256)
        try:
            return await asyncio.to_thread(
                partial(
                    patch_skill_frontmatter_document,
                    content,
                    expected_source_sha256=expected_source_sha256,
                    required_secrets=tuple(required_secrets),
                    secrets_autonomous=secrets_autonomous,
                )
            )
        except SkillFrontmatterSourceMismatch:
            raise SkillFrontmatterSourceStale(actor.request_id) from None
        except SkillFrontmatterPatchRejected as exc:
            raise SkillSecretDeclarationInvalid(
                actor.request_id,
                tuple(exc.diagnostics),
            ) from None
        except (TypeError, ValueError):
            raise SkillSecretDeclarationInvalid(actor.request_id) from None

    @staticmethod
    def _require_capability(
        actor: ProjectContext,
        capability: Capability,
    ) -> None:
        if not isinstance(actor, ProjectContext) or capability not in actor.capabilities:
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _validate_input(
        actor: ProjectContext,
        content: str,
        expected_source_sha256: str,
    ) -> None:
        if not isinstance(content, str) or not isinstance(expected_source_sha256, str) or _SHA256_PATTERN.fullmatch(expected_source_sha256) is None:
            raise AssetValidationFailed(actor.request_id)
        try:
            content_size = len(content.encode("utf-8"))
        except UnicodeError:
            raise SkillSecretDeclarationInvalid(actor.request_id) from None
        if content_size > MAX_SKILL_FRONTMATTER_DOCUMENT_BYTES:
            raise SkillSecretDeclarationInvalid(actor.request_id)
