"""Fail when AGENTS.md quotes a constant that no longer matches its source.

Each entry pins one documented number to the code or config that defines it, so a
changed constant names the exact guide sentence to update instead of letting the
specification drift silently away from the implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.shared_assets.skill_archive import (
    MAX_SKILL_ARCHIVE_BYTES,
    MAX_SKILL_ARCHIVE_FILE_BYTES,
    MAX_SKILL_ARCHIVE_FILES,
    MAX_SKILL_ARCHIVE_UPLOAD_BYTES,
)
from deerflow.agents.memory.snip import MAX_CONTINUITY_CHARS, MAX_SNIP_OUTPUT_CHARS
from deerflow.agents.middlewares.summarization_middleware import (
    MIN_SNIP_SUMMARY_OUTPUT_TOKENS,
)
from deerflow.agents.middlewares.view_image_middleware import _MAX_CURRENT_UPLOAD_IMAGES
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.config.quota_config import QuotaConfig
from deerflow.config.skills_config import SkillsConfig
from deerflow.config.subagents_config import (
    MAX_CONCURRENT_SUBAGENT_CALLS,
    MAX_TOTAL_SUBAGENTS_PER_RUN,
    MIN_CONCURRENT_SUBAGENT_CALLS,
    MIN_TOTAL_SUBAGENTS_PER_RUN,
)
from deerflow.config.vision_bridge_config import (
    DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS,
)
from deerflow.config.worker_config import WorkerStreamConfig
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.private_work.memory_document_repository import (
    DREAM_HISTORY_BATCH_SIZE,
    MEMORY_REVIEW_DELETION_RATIO,
    MEMORY_REVIEW_MIN_LINES,
    REMEMBER_BACKLOG_LIMIT,
    REMEMBER_RUN_LIMIT,
    TOOL_ENTRY_DUE_MINUTES,
)
from deerflow.skills.skillscan.orchestrator import (
    _MAX_ARCHIVE_MEMBERS,
    MAX_TOTAL_ARCHIVE_BYTES,
)
from deerflow.tools.builtins.view_image_tool import _MAX_IMAGE_BYTES

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_GUIDE = REPO_ROOT / "backend/AGENTS.md"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"

MIB = 1024 * 1024
GIB = 1024 * MIB
_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


@dataclass(frozen=True)
class DocumentedConstant:
    """One value quoted in a guide, plus the definition it must agree with."""

    pattern: str
    """Regex with exactly one group capturing the value as the guide renders it."""

    expected: str
    source: str
    doc: Path = BACKEND_GUIDE


def _spelled(count: int) -> str:
    return _NUMBER_WORDS[count] if count < len(_NUMBER_WORDS) else str(count)


def _field_bounds(field_name: str) -> str:
    metadata = MemoryConfig.model_fields[field_name].metadata
    low = next(item.ge for item in metadata if hasattr(item, "ge"))
    high = next(item.le for item in metadata if hasattr(item, "le"))
    return f"{low}..{high}"


def _example_config_version() -> str:
    match = re.search(r"(?m)^config_version:\s*(\d+)\s*$", EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    assert match, "config.example.yaml must declare a top-level config_version"
    return match.group(1)


_QUOTAS = QuotaConfig()

DOCUMENTED_CONSTANTS = (
    DocumentedConstant(
        pattern=r"current marker is `([a-z0-9_]+)`",
        expected=CURRENT_SCHEMA_REVISION,
        source="deerflow.persistence.bootstrap.CURRENT_SCHEMA_REVISION",
    ),
    DocumentedConstant(
        pattern=r"`config_version: (\d+)`",
        expected=_example_config_version(),
        source="config.example.yaml config_version",
    ),
    DocumentedConstant(
        pattern=r"Version (\d+) is the initial",
        expected=_example_config_version(),
        source="config.example.yaml config_version",
    ),
    DocumentedConstant(
        pattern=r"concurrency is canonically clamped to `(\d+\.\.\d+)`",
        expected=f"{MIN_CONCURRENT_SUBAGENT_CALLS}..{MAX_CONCURRENT_SUBAGENT_CALLS}",
        source="deerflow.config.subagents_config concurrent-call bounds",
    ),
    DocumentedConstant(
        pattern=r"per-Run total remains independently bounded to `(\d+\.\.\d+)`",
        expected=f"{MIN_TOTAL_SUBAGENTS_PER_RUN}..{MAX_TOTAL_SUBAGENTS_PER_RUN}",
        source="deerflow.config.subagents_config per-Run total bounds",
    ),
    DocumentedConstant(
        pattern=r"retaining the final (\d+) MiB",
        expected=str(MAX_TOTAL_ARCHIVE_BYTES // MIB),
        source="deerflow.skills.skillscan.orchestrator.MAX_TOTAL_ARCHIVE_BYTES",
    ),
    DocumentedConstant(
        pattern=r"(\d+)-file, bounded-log",
        expected=str(_MAX_ARCHIVE_MEMBERS),
        source="deerflow.skills.skillscan.orchestrator._MAX_ARCHIVE_MEMBERS",
    ),
    DocumentedConstant(
        pattern=r"archives remain limited to (\d+) MiB",
        expected=str(MAX_SKILL_ARCHIVE_BYTES // MIB),
        source="app.shared_assets.skill_archive.MAX_SKILL_ARCHIVE_BYTES",
    ),
    DocumentedConstant(
        pattern=r"archives remain limited to \d+ MiB total, (\d+) MiB\s+per regular",
        expected=str(MAX_SKILL_ARCHIVE_FILE_BYTES // MIB),
        source="app.shared_assets.skill_archive.MAX_SKILL_ARCHIVE_FILE_BYTES",
    ),
    DocumentedConstant(
        pattern=r"file, and (\d+) regular files",
        expected=str(MAX_SKILL_ARCHIVE_FILES),
        source="app.shared_assets.skill_archive.MAX_SKILL_ARCHIVE_FILES",
    ),
    DocumentedConstant(
        pattern=r"scoped\s+(\d+) MiB\s+wire limit",
        expected=str(MAX_SKILL_ARCHIVE_UPLOAD_BYTES // MIB),
        source="app.shared_assets.skill_archive.MAX_SKILL_ARCHIVE_UPLOAD_BYTES",
    ),
    DocumentedConstant(
        pattern=r"injects at most (\w+) unique images",
        expected=_spelled(_MAX_CURRENT_UPLOAD_IMAGES),
        source="deerflow.agents.middlewares.view_image_middleware._MAX_CURRENT_UPLOAD_IMAGES",
    ),
    DocumentedConstant(
        pattern=r"unique images with a (\d+) MiB per-image",
        expected=str(_MAX_IMAGE_BYTES // MIB),
        source="deerflow.tools.builtins.view_image_tool._MAX_IMAGE_BYTES",
    ),
    DocumentedConstant(
        pattern=r"`inspect_image` end-to-end deadline at (\d+) seconds",
        expected=str(DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS),
        source="deerflow.config.vision_bridge_config.DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS",
    ),
    DocumentedConstant(
        pattern=r"`skills\.read_evidence_ttl_calls` subsequent lead model calls \(default (\d+)",
        expected=str(SkillsConfig.model_fields["read_evidence_ttl_calls"].default),
        source="deerflow.config.skills_config.SkillsConfig.read_evidence_ttl_calls default",
    ),
    DocumentedConstant(
        pattern=r"free-prose task continuity bounded to ([\d,]+)\s+characters",
        expected=f"{MAX_CONTINUITY_CHARS:,}",
        source="deerflow.agents.memory.snip.MAX_CONTINUITY_CHARS",
    ),
    DocumentedConstant(
        pattern=r"declared output cap below ([\d,]+) tokens",
        expected=f"{MIN_SNIP_SUMMARY_OUTPUT_TOKENS:,}",
        source="deerflow.agents.middlewares.summarization_middleware.MIN_SNIP_SUMMARY_OUTPUT_TOKENS",
    ),
    DocumentedConstant(
        pattern=r"tagged fact lines bounded to ([\d,]+) characters",
        expected=f"{MAX_SNIP_OUTPUT_CHARS:,}",
        source="deerflow.agents.memory.snip.MAX_SNIP_OUTPUT_CHARS",
    ),
    DocumentedConstant(
        pattern=r"`worker\.stream\.text_delta_flush_ms`, default (\d+)ms",
        expected=str(
            WorkerStreamConfig.model_fields["text_delta_flush_ms"].default,
        ),
        source="deerflow.config.worker_config.WorkerStreamConfig.text_delta_flush_ms default",
    ),
    DocumentedConstant(
        pattern=r"`worker\.stream\.run_event_notify_enabled` is (true|false) \(the default\)",
        expected=str(
            WorkerStreamConfig.model_fields["run_event_notify_enabled"].default,
        ).lower(),
        source="deerflow.config.worker_config.WorkerStreamConfig.run_event_notify_enabled default",
    ),
    DocumentedConstant(
        pattern=r"`mcp_security\.run_session_reuse` \(default `(true|false)`\)",
        expected=str(
            McpSecurityConfig.model_fields["run_session_reuse"].default,
        ).lower(),
        source="deerflow.config.mcp_security_config.McpSecurityConfig.run_session_reuse default",
    ),
    DocumentedConstant(
        pattern=r"`dream_interval_minutes` \(`(\d+\.\.\d+)`",
        expected=_field_bounds("dream_interval_minutes"),
        source="deerflow.config.memory_config.MemoryConfig.dream_interval_minutes bounds",
    ),
    DocumentedConstant(
        pattern=r"`dream_interval_minutes` \(`\d+\.\.\d+`, default `(\d+)`\)",
        expected=str(MemoryConfig.model_fields["dream_interval_minutes"].default),
        source="deerflow.config.memory_config.MemoryConfig.dream_interval_minutes default",
    ),
    DocumentedConstant(
        pattern=r"`idle_seal_minutes` \(`(\d+)` or `\d+\.\.\d+`",
        expected=str(next(item.ge for item in MemoryConfig.model_fields["idle_seal_minutes"].metadata if hasattr(item, "ge"))),
        source="deerflow.config.memory_config.MemoryConfig.idle_seal_minutes lower bound",
    ),
    DocumentedConstant(
        pattern=r"`idle_seal_minutes` \(`\d+` or `\d+\.\.(\d+)`",
        expected=str(next(item.le for item in MemoryConfig.model_fields["idle_seal_minutes"].metadata if hasattr(item, "le"))),
        source="deerflow.config.memory_config.MemoryConfig.idle_seal_minutes upper bound",
    ),
    DocumentedConstant(
        pattern=r"`idle_seal_minutes` \(`\d+` or `\d+\.\.\d+`, default `(\d+)`",
        expected=str(MemoryConfig.model_fields["idle_seal_minutes"].default),
        source="deerflow.config.memory_config.MemoryConfig.idle_seal_minutes default",
    ),
    DocumentedConstant(
        pattern=r"`episode_retention_days` \(`(\d+)` or `\d+\.\.\d+`",
        expected=str(next(item.ge for item in MemoryConfig.model_fields["episode_retention_days"].metadata if hasattr(item, "ge"))),
        source="deerflow.config.memory_config.MemoryConfig.episode_retention_days lower bound",
    ),
    DocumentedConstant(
        pattern=r"`episode_retention_days` \(`\d+` or `\d+\.\.(\d+)`",
        expected=str(next(item.le for item in MemoryConfig.model_fields["episode_retention_days"].metadata if hasattr(item, "le"))),
        source="deerflow.config.memory_config.MemoryConfig.episode_retention_days upper bound",
    ),
    DocumentedConstant(
        pattern=r"`episode_retention_days` \(`\d+` or `\d+\.\.\d+`, default `(\d+)`",
        expected=str(MemoryConfig.model_fields["episode_retention_days"].default),
        source="deerflow.config.memory_config.MemoryConfig.episode_retention_days default",
    ),
    DocumentedConstant(
        pattern=r"strictly oldest (\d+) pending history rows",
        expected=str(DREAM_HISTORY_BATCH_SIZE),
        source="memory_document_repository.DREAM_HISTORY_BATCH_SIZE",
    ),
    DocumentedConstant(
        pattern=r"full batch of (\d+) is\s+already pending",
        expected=str(DREAM_HISTORY_BATCH_SIZE),
        source="memory_document_repository.DREAM_HISTORY_BATCH_SIZE (due rule)",
    ),
    DocumentedConstant(
        pattern=r"pending for over (\d+) minutes",
        expected=str(TOOL_ENTRY_DUE_MINUTES),
        source="memory_document_repository.TOOL_ENTRY_DUE_MINUTES",
    ),
    DocumentedConstant(
        pattern=r"per-Run audit cap of (\d+)",
        expected=str(REMEMBER_RUN_LIMIT),
        source="memory_document_repository.REMEMBER_RUN_LIMIT (recall audit)",
    ),
    DocumentedConstant(
        pattern=r"per-Run cap of (\d+)",
        expected=str(REMEMBER_RUN_LIMIT),
        source="memory_document_repository.REMEMBER_RUN_LIMIT",
    ),
    DocumentedConstant(
        pattern=r"pending-backlog cap of (\d+)",
        expected=str(REMEMBER_BACKLOG_LIMIT),
        source="memory_document_repository.REMEMBER_BACKLOG_LIMIT",
    ),
    DocumentedConstant(
        pattern=r"at least (\d+) content lines",
        expected=str(MEMORY_REVIEW_MIN_LINES),
        source="memory_document_repository.MEMORY_REVIEW_MIN_LINES",
    ),
    DocumentedConstant(
        pattern=r"over\s+(\d+)% of them vanished",
        expected=str(int(MEMORY_REVIEW_DELETION_RATIO * 100)),
        source="memory_document_repository.MEMORY_REVIEW_DELETION_RATIO",
    ),
    DocumentedConstant(
        pattern=r"defaults are (\d+) members",
        expected=str(_QUOTAS.default_member_limit),
        source="deerflow.config.quota_config.QuotaConfig.default_member_limit",
    ),
    DocumentedConstant(
        pattern=r"members, (\d+) GiB storage",
        expected=str(_QUOTAS.default_storage_bytes_limit // GIB),
        source="deerflow.config.quota_config.QuotaConfig.default_storage_bytes_limit",
    ),
    DocumentedConstant(
        pattern=r"GiB storage, (\d+) concurrent Runs",
        expected=str(_QUOTAS.default_concurrent_run_limit),
        source="deerflow.config.quota_config.QuotaConfig.default_concurrent_run_limit",
    ),
    DocumentedConstant(
        pattern=r"and ([\d,]+) MCP calls per UTC day",
        expected=f"{_QUOTAS.default_mcp_calls_daily_limit:,}",
        source="deerflow.config.quota_config.QuotaConfig.default_mcp_calls_daily_limit",
    ),
)


@pytest.mark.parametrize("constant", DOCUMENTED_CONSTANTS, ids=[item.source for item in DOCUMENTED_CONSTANTS])
def test_documented_constant_matches_its_source(constant: DocumentedConstant) -> None:
    documented = re.findall(constant.pattern, constant.doc.read_text(encoding="utf-8"))
    assert documented, f"{constant.doc.name} no longer has a sentence matching {constant.pattern!r}. Rewording the guide is fine, but update this guard in the same change."
    stale = sorted(set(documented) - {constant.expected})
    assert not stale, f"{constant.doc.name} documents {stale} in the sentence matching {constant.pattern!r}, but {constant.source} is {constant.expected!r}. Update the guide to match the implementation."


def test_every_guarded_pattern_is_unique() -> None:
    """A duplicated pattern would silently drop one of the two guarded values."""
    patterns = [item.pattern for item in DOCUMENTED_CONSTANTS]
    assert len(set(patterns)) == len(patterns)
