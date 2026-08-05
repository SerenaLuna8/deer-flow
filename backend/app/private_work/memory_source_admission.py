"""Admit immutable Memory v2 source evidence from a successful private Run."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.run_repository import PrivateRunRecord
from app.reliability.jobs import memory_extract_idempotency_key
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from app.system_runtime_settings.repository import (
    SystemRuntimePolicyRepository,
)
from app.system_runtime_settings.validation import canonical_policy_payload
from app.system_settings.repository import SystemModelRepository
from deerflow.persistence.jobs.model import JobAttemptRow
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.memory_v2_repository import (
    MemorySourceAdmissionRecord,
    MemorySourceAdmissionWrite,
    MemorySourceItemWrite,
    MemoryV2Repository,
)
from deerflow.runtime.private_scope import PrivateResourceScope

_USER_MESSAGE_TYPES = frozenset({"human", "user"})
_LEADING_UPLOAD_BLOCK_RE = re.compile(
    r"\A\s*(?:(?:<uploaded_files>[\s\S]*?</uploaded_files>)|(?:<current_uploads>[\s\S]*?</current_uploads>))\s*",
    re.IGNORECASE,
)
_DIRECT_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bauthorization\s*:\s*basic\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(?:sk|pk|tp)-(?:proj-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_LABELED_SECRET = re.compile(
    r"(?i)(?:^|[\s,;])(?:password|passwd|pwd|token|api[ _-]?key|access[ _-]?token|"
    r"client[ _-]?secret|secret|credential|密码|口令|令牌|密钥|凭据)\s*(?:=|:|：)\s*\S{4,}",
)
_MAX_SOURCE_ITEM_CHARS = 64_000
_SOURCE_ITEM_HMAC_DOMAIN = b"deerflow.memory.source-item.v1\x00"
_SOURCE_IDENTITY_VERSION = "memory-source-identity-v1"
_SOURCE_BATCH_ID_NAMESPACE = uuid.UUID("b8234c20-0a08-5a5a-a310-20db22f76c42")
MEMORY_EXTRACT_PROMPT_VERSION = "memory-extract-prompt-v3"
MEMORY_EXTRACTOR_VERSION = "memory-extractor-v2"
MEMORY_EXTRACT_OUTPUT_SCHEMA_VERSION = "memory-candidate-v1"


class SourceHmacRef(Protocol):
    key_id: str
    hmac_hex: str


MemorySourceHmac = Callable[[bytes], SourceHmacRef]
MemorySourceHmacRefs = Callable[[bytes], tuple[SourceHmacRef, ...]]


@dataclass(frozen=True, slots=True)
class PreparedMemorySourceItem:
    ordinal: int
    source_message_id: str
    content: str
    content_hmac: str
    suppression_refs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PreparedMemorySource:
    items: tuple[PreparedMemorySourceItem, ...]
    source_identity_digest: str
    hmac_key_version: str


class MemorySourceAdmissionPort(Protocol):
    async def admit_successful_run(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        run: PrivateRunRecord,
        source_job_id: uuid.UUID,
        source_attempt_id: uuid.UUID,
    ) -> MemorySourceAdmissionRecord | None: ...


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _input_messages(raw_input: object) -> list[object]:
    if isinstance(raw_input, Mapping):
        messages = raw_input.get("messages")
    else:
        messages = raw_input
    return messages if isinstance(messages, list) else []


def _run_source_input(run: PrivateRunRecord) -> object:
    command = run.kwargs.get("command")
    if command is not None:
        if not isinstance(command, Mapping):
            return None
        update = command.get("update")
        if not isinstance(update, Mapping):
            return None
        return {"messages": update.get("messages")}
    return run.kwargs.get("input")


def _message_value(message: object, key: str) -> object:
    if isinstance(message, Mapping):
        return message.get(key)
    return getattr(message, key, None)


def _is_visible_user_message(message: object) -> bool:
    discriminators = [_message_value(message, key) for key in ("role", "type") if _message_value(message, key) is not None]
    if not discriminators or not all(isinstance(value, str) and value.lower() in _USER_MESSAGE_TYPES for value in discriminators):
        return False
    name = _message_value(message, "name")
    if isinstance(name, str) and name:
        return False
    additional = _message_value(message, "additional_kwargs")
    if not isinstance(additional, Mapping):
        return True
    return (
        not any(
            additional.get(key) is not None
            for key in (
                "human_input_response",
                "conversation_quote_context",
                "sidecar_context",
            )
        )
        and additional.get("hide_from_ui") is not True
    )


def _message_text(message: object) -> str:
    content = _message_value(message, "content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        text = block.get("text")
        if isinstance(text, str) and (block_type is None or block_type in {"text", "input_text"}):
            parts.append(text)
    return "".join(parts)


def _filtered_text(message: object) -> str | None:
    text = _message_text(message).replace("\r\n", "\n").replace("\r", "\n")
    while _LEADING_UPLOAD_BLOCK_RE.match(text):
        text = _LEADING_UPLOAD_BLOCK_RE.sub("", text, count=1)
    text = text.strip()
    if not text or len(text) > _MAX_SOURCE_ITEM_CHARS:
        return None
    if _LABELED_SECRET.search(text) or any(pattern.search(text) for pattern in _DIRECT_SECRET_PATTERNS):
        return None
    return text


def _usable_message_id(value: object, seen: set[str]) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        return None
    if any(ord(character) < 32 for character in value) or value in seen:
        return None
    return value


def prepare_memory_source(
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    namespace: str,
    run_id: str,
    source_attempt_id: uuid.UUID,
    raw_input: object,
    source_hmac: MemorySourceHmac,
    source_hmac_refs: MemorySourceHmacRefs | None = None,
) -> PreparedMemorySource | None:
    """Return filtered, ordered source evidence or ``None`` when nothing qualifies."""

    try:
        normalized_project_id = str(uuid.UUID(str(project_id)))
        normalized_owner_id = str(uuid.UUID(owner_user_id))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Memory source scope is invalid") from None
    if (
        not isinstance(namespace, str)
        or not namespace
        or namespace != namespace.strip()
        or len(namespace) > 255
        or not isinstance(run_id, str)
        or not run_id
        or len(run_id) > 64
        or type(source_attempt_id) is not uuid.UUID
        or not callable(source_hmac)
        or (source_hmac_refs is not None and not callable(source_hmac_refs))
    ):
        raise ValueError("Memory source coordinates are invalid")

    items: list[PreparedMemorySourceItem] = []
    seen_message_ids: set[str] = set()
    key_version: str | None = None
    for source_position, message in enumerate(_input_messages(raw_input)):
        if not _is_visible_user_message(message):
            continue
        content = _filtered_text(message)
        if content is None:
            continue
        original_message_id = _usable_message_id(
            _message_value(message, "id"),
            seen_message_ids,
        )
        hmac_payload = _canonical_bytes(
            {
                "content": content,
                "namespace": namespace,
                "owner_user_id": normalized_owner_id,
                "project_id": normalized_project_id,
                "run_id": run_id,
                "source_message_id": original_message_id,
                "source_position": source_position,
            }
        )
        reference = source_hmac(_SOURCE_ITEM_HMAC_DOMAIN + hmac_payload)
        if not isinstance(reference.key_id, str) or not reference.key_id or len(reference.key_id) > 64 or re.fullmatch(r"[0-9a-f]{64}", reference.hmac_hex) is None:
            raise ValueError("Memory source HMAC is invalid")
        retained_references = (reference,) if source_hmac_refs is None else source_hmac_refs(_SOURCE_ITEM_HMAC_DOMAIN + hmac_payload)
        if (
            not isinstance(retained_references, tuple)
            or not retained_references
            or retained_references[0].key_id != reference.key_id
            or retained_references[0].hmac_hex != reference.hmac_hex
            or len({item.key_id for item in retained_references}) != len(retained_references)
            or any(not isinstance(item.key_id, str) or not item.key_id or len(item.key_id) > 64 or re.fullmatch(r"[0-9a-f]{64}", item.hmac_hex) is None for item in retained_references)
        ):
            raise ValueError("Memory source HMAC keyring is invalid")
        if key_version is None:
            key_version = reference.key_id
        elif key_version != reference.key_id:
            raise ValueError("Memory source HMAC key changed during admission")

        message_id = original_message_id
        if message_id is None:
            fallback_digest = hashlib.sha256(
                _canonical_bytes(
                    {
                        "content_hmac": reference.hmac_hex,
                        "run_id": run_id,
                        "source_position": source_position,
                    }
                )
            ).hexdigest()
            message_id = f"generated:{fallback_digest}"
        seen_message_ids.add(message_id)
        items.append(
            PreparedMemorySourceItem(
                ordinal=len(items),
                source_message_id=message_id,
                content=content,
                content_hmac=reference.hmac_hex,
                suppression_refs=tuple((item.key_id, item.hmac_hex) for item in retained_references),
            )
        )

    if not items or key_version is None:
        return None
    source_identity_digest = hashlib.sha256(
        _canonical_bytes(
            {
                "attempt_id": str(source_attempt_id),
                "items": [
                    {
                        "content_hmac": item.content_hmac,
                        "source_message_id": item.source_message_id,
                    }
                    for item in items
                ],
                "namespace": namespace,
                "owner_user_id": normalized_owner_id,
                "project_id": normalized_project_id,
                "run_id": run_id,
                "version": _SOURCE_IDENTITY_VERSION,
            }
        )
    ).hexdigest()
    return PreparedMemorySource(
        items=tuple(items),
        source_identity_digest=source_identity_digest,
        hmac_key_version=key_version,
    )


def _is_non_interactive_run(run: PrivateRunRecord) -> bool:
    config = run.kwargs.get("config")
    context = config.get("context") if isinstance(config, Mapping) else None
    return isinstance(context, Mapping) and context.get("non_interactive") is True


def _contract_digest(
    *,
    policy_version_id: uuid.UUID,
    policy_revision: int,
    policy_checksum: str,
    model_config_id: uuid.UUID,
    model_config_version_id: uuid.UUID,
    model_config_checksum: str,
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "extractor_version": MEMORY_EXTRACTOR_VERSION,
                "model_config_checksum": model_config_checksum,
                "model_config_id": str(model_config_id),
                "model_config_version_id": str(model_config_version_id),
                "output_schema_version": MEMORY_EXTRACT_OUTPUT_SCHEMA_VERSION,
                "policy_checksum": policy_checksum,
                "policy_revision": policy_revision,
                "policy_version_id": str(policy_version_id),
                "prompt_version": MEMORY_EXTRACT_PROMPT_VERSION,
            }
        )
    ).hexdigest()


class MemorySourceAdmissionService:
    """Create Memory v2 source work inside a successful Run settlement."""

    def __init__(
        self,
        *,
        source_hmac: MemorySourceHmac,
        source_hmac_refs: MemorySourceHmacRefs | None = None,
        job_repository_builder=JobRepository,
        repository_builder=MemoryV2Repository,
        personalization_repository_builder=AccountPersonalizationRepository,
        namespace: str = "default",
        extract_job_max_attempts: int = 3,
    ) -> None:
        if (
            not callable(source_hmac)
            or (source_hmac_refs is not None and not callable(source_hmac_refs))
            or not callable(job_repository_builder)
            or not callable(repository_builder)
            or not callable(personalization_repository_builder)
            or not isinstance(namespace, str)
            or not namespace
            or namespace != namespace.strip()
            or len(namespace) > 255
            or not 1 <= extract_job_max_attempts <= 20
        ):
            raise ValueError("Memory source admission configuration is invalid")
        self._source_hmac = source_hmac
        self._source_hmac_refs = source_hmac_refs
        self._job_repository_builder = job_repository_builder
        self._repository_builder = repository_builder
        self._personalization_repository_builder = personalization_repository_builder
        self._namespace = namespace
        self._extract_job_max_attempts = extract_job_max_attempts

    @staticmethod
    async def _require_successful_attempt(
        session: AsyncSession,
        *,
        source_job_id: uuid.UUID,
        source_attempt_id: uuid.UUID,
    ) -> None:
        attempt = await session.scalar(
            select(JobAttemptRow.id)
            .where(
                JobAttemptRow.id == source_attempt_id,
                JobAttemptRow.job_id == source_job_id,
                JobAttemptRow.outcome == "succeeded",
            )
            .with_for_update(read=True, of=JobAttemptRow)
        )
        if attempt is None:
            raise RuntimeError("Successful Memory source attempt is unavailable")

    async def admit_successful_run(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        run: PrivateRunRecord,
        source_job_id: uuid.UUID,
        source_attempt_id: uuid.UUID,
    ) -> MemorySourceAdmissionRecord | None:
        if (
            type(scope) is not PrivateResourceScope
            or type(run) is not PrivateRunRecord
            or run.status != "success"
            or run.project_id != uuid.UUID(scope.project_id)
            or run.owner_user_id != scope.owner_user_id
            or run.job_id != source_job_id
            or type(source_attempt_id) is not uuid.UUID
        ):
            raise RuntimeError("Successful Memory source Run is invalid")
        await self._require_successful_attempt(
            session,
            source_job_id=source_job_id,
            source_attempt_id=source_attempt_id,
        )
        if _is_non_interactive_run(run):
            return None
        preference = await self._personalization_repository_builder(session).read_memory(run.owner_user_id, for_update=True)
        if not preference.memory_enabled:
            return None

        policy_material = await SystemRuntimePolicyRepository(session).snapshot_material(
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            run_id=run.run_id,
            section=RuntimePolicySection.AGENT_RUNTIME,
        )
        if policy_material is None:
            raise RuntimeError("Memory source policy snapshot is unavailable")
        policy_snapshot, policy_version = policy_material
        canonical_policy = canonical_policy_payload(
            RuntimePolicySection.AGENT_RUNTIME,
            dict(policy_version.value),
        )
        policy = canonical_policy.value
        if (
            canonical_policy.schema_version != policy_snapshot.schema_version
            or canonical_policy.schema_version != policy_version.schema_version
            or canonical_policy.checksum != policy_snapshot.payload_checksum
            or canonical_policy.checksum != policy_version.payload_checksum
        ):
            raise RuntimeError("Memory source policy snapshot is invalid")
        policy = AgentRuntimePolicyValue.model_validate(policy)
        if not isinstance(policy, AgentRuntimePolicyValue):
            raise RuntimeError("Memory source policy snapshot is invalid")
        if not policy.memory.enabled or policy.memory.pipeline_mode == "off":
            return None

        prepared = prepare_memory_source(
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            namespace=self._namespace,
            run_id=run.run_id,
            source_attempt_id=source_attempt_id,
            raw_input=_run_source_input(run),
            source_hmac=self._source_hmac,
            source_hmac_refs=self._source_hmac_refs,
        )
        if prepared is None:
            return None

        repository = self._repository_builder(
            session,
            jobs=self._job_repository_builder(session),
        )
        suppression_refs: dict[str, set[str]] = {}
        for item in prepared.items:
            for key_id, hmac_hex in item.suppression_refs:
                suppression_refs.setdefault(key_id, set()).add(hmac_hex)
        for key_id, identity_hmacs in suppression_refs.items():
            if await repository.source_suppressed(
                project_id=run.project_id,
                owner_user_id=run.owner_user_id,
                namespace=self._namespace,
                hmac_key_version=key_id,
                identity_hmacs=tuple(sorted(identity_hmacs)),
            ):
                return None

        model_purpose = "memory" if policy.memory.model_name is not None else "lead"
        model_snapshot = await SystemModelRepository(session).existing_snapshot(
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            run_id=run.run_id,
            purpose=model_purpose,
        )
        if model_snapshot is None or (policy.memory.model_name is not None and model_snapshot.logical_name != policy.memory.model_name):
            raise RuntimeError("Memory source model snapshot is unavailable")

        contract_digest = _contract_digest(
            policy_version_id=policy_snapshot.policy_version_id,
            policy_revision=int(policy_version.version_number),
            policy_checksum=policy_snapshot.payload_checksum,
            model_config_id=model_snapshot.model_config_id,
            model_config_version_id=model_snapshot.model_config_version_id,
            model_config_checksum=model_snapshot.payload_checksum,
        )
        source_batch_id = uuid.uuid5(
            _SOURCE_BATCH_ID_NAMESPACE,
            "\x00".join(
                (
                    str(run.project_id),
                    run.owner_user_id,
                    self._namespace,
                    run.run_id,
                    str(source_attempt_id),
                    prepared.source_identity_digest,
                )
            ),
        )
        request = MemorySourceAdmissionWrite(
            source_batch_id=source_batch_id,
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            namespace=self._namespace,
            thread_id=run.thread_id,
            run_id=run.run_id,
            source_job_id=source_job_id,
            source_attempt_id=source_attempt_id,
            pipeline_mode=policy.memory.pipeline_mode,
            policy_section=policy_snapshot.section,
            policy_version_id=policy_snapshot.policy_version_id,
            policy_schema_version=policy_snapshot.schema_version,
            policy_checksum=policy_snapshot.payload_checksum,
            policy_revision=int(policy_version.version_number),
            source_identity_digest=prepared.source_identity_digest,
            source_hmac_key_version=prepared.hmac_key_version,
            items=tuple(
                MemorySourceItemWrite(
                    ordinal=item.ordinal,
                    source_message_id=item.source_message_id,
                    content=item.content,
                    content_hmac=item.content_hmac,
                )
                for item in prepared.items
            ),
            extract_job_idempotency_key=memory_extract_idempotency_key(
                source_batch_id,
                contract_digest,
            ),
            contract_digest=contract_digest,
            model_config_id=model_snapshot.model_config_id,
            model_config_version_id=model_snapshot.model_config_version_id,
            model_config_checksum=model_snapshot.payload_checksum,
            prompt_version=MEMORY_EXTRACT_PROMPT_VERSION,
            extractor_version=MEMORY_EXTRACTOR_VERSION,
            output_schema_version=MEMORY_EXTRACT_OUTPUT_SCHEMA_VERSION,
            extract_job_max_attempts=self._extract_job_max_attempts,
        )
        return await repository.admit_source(request)


__all__ = [
    "MEMORY_EXTRACTOR_VERSION",
    "MEMORY_EXTRACT_OUTPUT_SCHEMA_VERSION",
    "MEMORY_EXTRACT_PROMPT_VERSION",
    "MemorySourceAdmissionPort",
    "MemorySourceAdmissionService",
    "MemorySourceHmac",
    "MemorySourceHmacRefs",
    "PreparedMemorySource",
    "PreparedMemorySourceItem",
    "prepare_memory_source",
]
