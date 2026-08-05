"""Worker-only execution of durable Memory v2 extraction Jobs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountMemoryPreference,
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.private_work.memory_source_admission import (
    MEMORY_EXTRACT_OUTPUT_SCHEMA_VERSION,
    MEMORY_EXTRACT_PROMPT_VERSION,
    MEMORY_EXTRACTOR_VERSION,
)
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.system_runtime_settings.materializer import (
    SystemRuntimePolicyMaterializer,
)
from app.system_settings import SystemModelMaterializer
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.agents.memory.extractor import (
    MemoryCandidateExtractor,
    MemoryExtractionError,
    MemoryExtractionSource,
    RunOneshotMemoryExtractionModelCaller,
)
from deerflow.config.app_config import AppConfig
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.private_work.memory_v2_repository import (
    MemoryCandidateDraft,
    MemoryCandidateWrite,
    MemoryExtractionWork,
    MemoryV2ExtractionConflict,
    MemoryV2ExtractionLeaseLost,
    MemoryV2Repository,
    prepare_memory_candidate_writes,
)

_MEMORY_EXTRACT_REQUEST_ID = "memory-extract-worker"


class MemoryExtractorPort(Protocol):
    async def extract(
        self,
        sources: tuple[MemoryExtractionSource, ...],
    ): ...


class MemoryScopeValidator(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        claim: JobClaim,
        *,
        lock: bool,
    ) -> bool: ...


async def _default_scope_validator(
    session: AsyncSession,
    claim: JobClaim,
    *,
    lock: bool,
) -> bool:
    owner_user_id = claim.scope.owner_user_id
    if owner_user_id is None:
        return False
    try:
        context = await resolve_project_context_in_transaction(
            session,
            uuid.UUID(owner_user_id),
            claim.scope.project_id,
            _MEMORY_EXTRACT_REQUEST_ID,
            lock=lock,
        )
        context.require(Capability.PRIVATE_WORK_CREATE)
        return True
    except (ProjectNotFound, ProjectForbidden, ValueError):
        return False


class MemoryExtractJobHandler:
    """Extract shadow Candidates without loading an Agent or current Memory."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        app_config: AppConfig | None,
        model_materializer: SystemModelMaterializer | None = None,
        runtime_policy_materializer: SystemRuntimePolicyMaterializer | None = None,
        extractor_factory: Callable[[object], MemoryExtractorPort] | None = None,
        repository_builder=MemoryV2Repository,
        job_repository_builder=JobRepository,
        scope_validator: MemoryScopeValidator | None = None,
        personalization_repository_builder=AccountPersonalizationRepository,
    ) -> None:
        if (
            not callable(session_factory)
            or not callable(repository_builder)
            or not callable(job_repository_builder)
            or not callable(personalization_repository_builder)
            or (extractor_factory is None and not isinstance(app_config, AppConfig))
        ):
            raise ValueError("Memory extract Worker configuration is invalid")
        self._sessions = session_factory
        self._app_config = app_config
        self._model_materializer = model_materializer or SystemModelMaterializer(
            session_factory,
        )
        self._runtime_policy_materializer = runtime_policy_materializer or SystemRuntimePolicyMaterializer(session_factory)
        self._extractor_factory = extractor_factory or self._make_extractor
        self._repository_builder = repository_builder
        self._job_repository_builder = job_repository_builder
        self._scope_validator = scope_validator or _default_scope_validator
        self._personalization_repository_builder = personalization_repository_builder

    def _make_extractor(self, model: object) -> MemoryCandidateExtractor:
        if self._app_config is None:
            raise RuntimeError("Memory extract app config is unavailable")
        model_name = getattr(model, "name", None)
        if not isinstance(model_name, str) or not model_name:
            raise RuntimeError("Memory extract model is invalid")
        runtime_config = self._app_config.with_runtime_models((model,))
        return MemoryCandidateExtractor(
            RunOneshotMemoryExtractionModelCaller(
                app_config=runtime_config,
                model_name=model_name,
            )
        )

    def _repository(self, session: AsyncSession) -> MemoryV2Repository:
        return self._repository_builder(
            session,
            jobs=self._job_repository_builder(session),
        )

    async def _load_work(
        self,
        claim: JobClaim,
    ) -> tuple[bool, AccountMemoryPreference | None, MemoryExtractionWork | None]:
        async with self._sessions() as session, session.begin():
            allowed = await self._scope_validator(
                session,
                claim,
                lock=False,
            )
            if not allowed:
                return False, None, None
            try:
                preference = await self._personalization_repository_builder(session).read_memory(claim.scope.owner_user_id or "")
            except AccountPersonalizationNotFound:
                return False, None, None
            work = await self._repository(session).load_extraction_work(
                job_id=claim.job_id,
                project_id=claim.scope.project_id,
                owner_user_id=claim.scope.owner_user_id or "",
                namespace=claim.namespace or "",
            )
        return True, preference, work

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement:
        if claim.job_type != "memory_extract" or claim.scope.owner_user_id is None or not claim.namespace or claim.run_id is not None or claim.occurrence_id is not None:
            return JobOutcome.cancelled()

        await authority.heartbeat()
        if authority.cancel_requested:
            return JobOutcome.cancelled()
        try:
            allowed, preference, work = await self._load_work(claim)
        except asyncio.CancelledError:
            raise
        except MemoryV2ExtractionConflict:
            return JobOutcome.failed("MEMORY_EXTRACT_WORK_INVALID")
        except Exception:
            return JobOutcome.failed("MEMORY_EXTRACT_WORK_UNAVAILABLE")
        if not allowed:
            return JobOutcome.cancelled()
        if work is None:
            return JobOutcome.failed("MEMORY_EXTRACT_WORK_UNAVAILABLE")
        if preference is None:
            return JobOutcome.cancelled()
        if not preference.memory_enabled:
            return self._settlement(
                claim,
                work,
                candidates=(),
                cancel=True,
                expected_preferences_version=preference.version,
            )
        if work.suppressed:
            return self._settlement(
                claim,
                work,
                candidates=(),
                cancel=True,
            )
        if work.cancel_requested:
            return self._settlement(
                claim,
                work,
                candidates=(),
                cancel=True,
            )
        if work.candidate_committed:
            return self._settlement(
                claim,
                work,
                candidates=(),
                cancel=False,
            )
        if work.prompt_version != MEMORY_EXTRACT_PROMPT_VERSION or work.extractor_version != MEMORY_EXTRACTOR_VERSION or work.output_schema_version != MEMORY_EXTRACT_OUTPUT_SCHEMA_VERSION:
            return JobOutcome.failed("MEMORY_EXTRACT_CONTRACT_UNSUPPORTED")

        try:
            policy = await self._runtime_policy_materializer.materialize_run_snapshot(
                project_id=work.project_id,
                owner_user_id=work.owner_user_id,
                run_id=work.run_id,
            )
            memory_policy = policy.memory
            if memory_policy.enabled is not True or memory_policy.pipeline_mode != work.pipeline_mode or memory_policy.pipeline_mode == "off":
                return JobOutcome.failed("MEMORY_EXTRACT_POLICY_INVALID")
            purpose = "memory" if memory_policy.model_name is not None else "lead"
            if not work.has_exact_model_snapshot(purpose):
                return JobOutcome.failed("MEMORY_EXTRACT_MODEL_INVALID")
            model = await self._model_materializer.materialize_snapshot(
                project_id=work.project_id,
                owner_user_id=work.owner_user_id,
                run_id=work.run_id,
                purpose=purpose,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return JobOutcome.failed("MEMORY_EXTRACT_MODEL_UNAVAILABLE")

        await authority.heartbeat()
        if authority.cancel_requested:
            return self._settlement(
                claim,
                work,
                candidates=(),
                cancel=True,
            )
        try:
            extractor = self._extractor_factory(model)
            result = await extractor.extract(
                tuple(
                    MemoryExtractionSource(
                        ordinal=item.ordinal,
                        content=item.content or "",
                    )
                    for item in work.source_items
                )
            )
            writes = prepare_memory_candidate_writes(
                work.generation_id,
                (
                    MemoryCandidateDraft(
                        source_ordinal=candidate.source_ordinal,
                        candidate_type=candidate.candidate_type,
                        content=candidate.content,
                        confidence=candidate.confidence,
                        retention_class=candidate.retention_class,
                        sensitivity=candidate.sensitivity,
                    )
                    for candidate in result.candidates
                ),
            )
        except asyncio.CancelledError:
            raise
        except MemoryExtractionError as error:
            return JobOutcome.failed(error.code)
        except (TypeError, ValueError):
            return JobOutcome.failed("MEMORY_EXTRACT_OUTPUT_INVALID")
        except Exception:
            return JobOutcome.failed("MEMORY_EXTRACT_UNAVAILABLE")

        await authority.heartbeat()
        if authority.cancel_requested:
            return self._settlement(
                claim,
                work,
                candidates=(),
                cancel=True,
            )
        return self._settlement(
            claim,
            work,
            candidates=writes,
            cancel=False,
            expected_preferences_version=preference.version,
        )

    def _settlement(
        self,
        claim: JobClaim,
        work: MemoryExtractionWork,
        *,
        candidates: tuple[MemoryCandidateWrite, ...],
        cancel: bool,
        expected_preferences_version: int | None = None,
    ) -> JobSettlement:
        async def commit() -> None:
            async with self._sessions() as session, session.begin():
                try:
                    preference = await self._personalization_repository_builder(session).read_memory(work.owner_user_id, for_update=True)
                except AccountPersonalizationNotFound:
                    preference = None
                personalization_allowed = preference is not None and preference.memory_enabled and (expected_preferences_version is None or preference.version == expected_preferences_version)
                allowed = await self._scope_validator(
                    session,
                    claim,
                    lock=True,
                )
                try:
                    await self._repository(session).finalize_extraction(
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        project_id=work.project_id,
                        owner_user_id=work.owner_user_id,
                        namespace=work.namespace,
                        generation_id=work.generation_id,
                        source_batch_id=work.source_batch_id,
                        contract_digest=work.contract_digest,
                        pipeline_mode=work.pipeline_mode,
                        candidates=(candidates if allowed and personalization_allowed else ()),
                        cancel=cancel or not allowed or not personalization_allowed,
                    )
                except MemoryV2ExtractionLeaseLost:
                    raise LeaseLost(claim.job_id) from None

        outcome = JobOutcome.cancelled() if cancel else JobOutcome.succeeded()
        return JobSettlement(outcome, commit)


__all__ = ["MemoryExtractJobHandler"]
