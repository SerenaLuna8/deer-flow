"""Worker-only Memory v2 consolidation and Candidate retention Jobs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from app.system_settings import SystemModelMaterializer
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.agents.memory.consolidator import (
    MEMORY_CONSOLIDATE_OUTPUT_SCHEMA_VERSION,
    MEMORY_CONSOLIDATE_PROMPT_VERSION,
    MEMORY_CONSOLIDATOR_VERSION,
    MemoryConsolidationCandidateInput,
    MemoryConsolidationError,
    MemoryConsolidationFactInput,
    MemoryConsolidator,
    RunOneshotMemoryConsolidationModelCaller,
)
from deerflow.config.app_config import AppConfig
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.private_work.memory_v2_repository import (
    MemoryConsolidationDecisionWrite,
    MemoryConsolidationWork,
    MemoryV2ConsolidationConflict,
    MemoryV2ConsolidationLeaseLost,
    MemoryV2Repository,
    MemoryV2RetentionConflict,
    MemoryV2RetentionLeaseLost,
)

_MEMORY_CONSOLIDATE_REQUEST_ID = "memory-consolidate-worker"
_MEMORY_RETENTION_REQUEST_ID = "memory-retention-worker"


class MemoryConsolidatorPort(Protocol):
    async def consolidate(self, candidates, facts): ...


class MemoryScopeValidator(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        claim: JobClaim,
        *,
        lock: bool,
    ) -> bool: ...


async def _validate_scope(
    session: AsyncSession,
    claim: JobClaim,
    *,
    lock: bool,
    request_id: str,
    capability: Capability,
) -> bool:
    owner_user_id = claim.scope.owner_user_id
    if owner_user_id is None:
        return False
    try:
        context = await resolve_project_context_in_transaction(
            session,
            uuid.UUID(owner_user_id),
            claim.scope.project_id,
            request_id,
            lock=lock,
        )
        context.require(capability)
        return True
    except (ProjectNotFound, ProjectForbidden, ValueError):
        return False


async def _default_consolidation_scope_validator(
    session: AsyncSession,
    claim: JobClaim,
    *,
    lock: bool,
) -> bool:
    return await _validate_scope(
        session,
        claim,
        lock=lock,
        request_id=_MEMORY_CONSOLIDATE_REQUEST_ID,
        capability=Capability.PRIVATE_WORK_CREATE,
    )


async def _default_retention_scope_validator(
    session: AsyncSession,
    claim: JobClaim,
    *,
    lock: bool,
) -> bool:
    return await _validate_scope(
        session,
        claim,
        lock=lock,
        request_id=_MEMORY_RETENTION_REQUEST_ID,
        capability=Capability.PRIVATE_WORK_READ_OWN,
    )


class MemoryConsolidateJobHandler:
    """Consolidate one frozen Candidate generation without loading an Agent."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        app_config: AppConfig | None,
        model_materializer: SystemModelMaterializer | None = None,
        runtime_policy_materializer: SystemRuntimePolicyMaterializer | None = None,
        consolidator_factory: Callable[[object], MemoryConsolidatorPort] | None = None,
        repository_builder=MemoryV2Repository,
        job_repository_builder=JobRepository,
        scope_validator: MemoryScopeValidator | None = None,
        retry_initial_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> None:
        if (
            not callable(session_factory)
            or not callable(repository_builder)
            or not callable(job_repository_builder)
            or (consolidator_factory is None and not isinstance(app_config, AppConfig))
            or type(retry_initial_seconds) is not int
            or type(retry_max_seconds) is not int
            or retry_initial_seconds < 1
            or retry_max_seconds < retry_initial_seconds
        ):
            raise ValueError("Memory consolidate Worker configuration is invalid")
        self._sessions = session_factory
        self._app_config = app_config
        self._model_materializer = model_materializer or SystemModelMaterializer(
            session_factory,
        )
        self._runtime_policy_materializer = runtime_policy_materializer or SystemRuntimePolicyMaterializer(session_factory)
        self._consolidator_factory = consolidator_factory or self._make_consolidator
        self._repository_builder = repository_builder
        self._job_repository_builder = job_repository_builder
        self._scope_validator = scope_validator or _default_consolidation_scope_validator
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds

    def _make_consolidator(self, model: object) -> MemoryConsolidator:
        if self._app_config is None:
            raise RuntimeError("Memory consolidate app config is unavailable")
        model_name = getattr(model, "name", None)
        if not isinstance(model_name, str) or not model_name:
            raise RuntimeError("Memory consolidate model is invalid")
        runtime_config = self._app_config.with_runtime_models((model,))
        return MemoryConsolidator(
            RunOneshotMemoryConsolidationModelCaller(
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
    ) -> tuple[bool, MemoryConsolidationWork | None]:
        async with self._sessions() as session, session.begin():
            allowed = await self._scope_validator(
                session,
                claim,
                lock=False,
            )
            if not allowed:
                return False, None
            work = await self._repository(session).load_consolidation_work(
                job_id=claim.job_id,
                project_id=claim.scope.project_id,
                owner_user_id=claim.scope.owner_user_id or "",
                namespace=claim.namespace or "",
            )
        return True, work

    @staticmethod
    def _is_supported_contract(work: MemoryConsolidationWork) -> bool:
        return work.prompt_version == MEMORY_CONSOLIDATE_PROMPT_VERSION and work.consolidator_version == MEMORY_CONSOLIDATOR_VERSION and work.output_schema_version == MEMORY_CONSOLIDATE_OUTPUT_SCHEMA_VERSION

    @staticmethod
    def _writes(
        work: MemoryConsolidationWork,
        decisions,
        *,
        max_facts: int,
        confidence_threshold: float,
    ) -> tuple[MemoryConsolidationDecisionWrite, ...]:
        by_candidate = {decision.candidate_id: decision for decision in decisions}
        facts = {fact.id: fact for fact in work.facts}
        available_fact_slots = max(0, max_facts - work.active_fact_count)
        admitted_create_signatures: set[tuple[str, str, str]] = set()
        revised_fact_ids: set[uuid.UUID] = set()
        writes: list[MemoryConsolidationDecisionWrite] = []
        for candidate in work.candidates:
            if candidate.sensitivity != "normal":
                writes.append(
                    MemoryConsolidationDecisionWrite(
                        candidate_id=candidate.id,
                        action="reject",
                        target_fact_id=None,
                        expected_revision_id=None,
                        content=None,
                        category=None,
                        confidence=None,
                        change_reason=None,
                        decision_reason="sensitive_content",
                    )
                )
                continue
            decision = by_candidate.get(candidate.id)
            if decision is None:
                raise ValueError("Memory consolidation decision is missing")
            mutates_fact = decision.action in {"create", "confirm", "revise"}
            policy_pending = mutates_fact and (candidate.retention_class == "ephemeral" or candidate.confidence < confidence_threshold or (decision.action in {"create", "revise"} and float(decision.confidence or 0) < confidence_threshold))
            if decision.action == "create" and not policy_pending:
                create_signature = (
                    candidate.candidate_type,
                    " ".join((decision.content or "").split()).casefold(),
                    " ".join((decision.category or "").split()).casefold(),
                )
                if create_signature in admitted_create_signatures:
                    pass
                elif available_fact_slots == 0:
                    policy_pending = True
                else:
                    available_fact_slots -= 1
                    admitted_create_signatures.add(create_signature)
            if decision.action == "revise" and not policy_pending:
                if decision.target_fact_id in revised_fact_ids:
                    writes.append(
                        MemoryConsolidationDecisionWrite(
                            candidate_id=candidate.id,
                            action="pending",
                            target_fact_id=None,
                            expected_revision_id=None,
                            content=None,
                            category=None,
                            confidence=None,
                            change_reason=None,
                            decision_reason="possible_conflict",
                        )
                    )
                    continue
                revised_fact_ids.add(decision.target_fact_id)
            if policy_pending:
                writes.append(
                    MemoryConsolidationDecisionWrite(
                        candidate_id=candidate.id,
                        action="pending",
                        target_fact_id=None,
                        expected_revision_id=None,
                        content=None,
                        category=None,
                        confidence=None,
                        change_reason=None,
                        decision_reason="insufficient_evidence",
                    )
                )
                continue
            target = None if decision.target_fact_id is None else facts.get(decision.target_fact_id)
            if decision.target_fact_id is not None and target is None:
                raise ValueError("Memory consolidation target Fact is missing")
            writes.append(
                MemoryConsolidationDecisionWrite(
                    candidate_id=candidate.id,
                    action=decision.action,
                    target_fact_id=decision.target_fact_id,
                    expected_revision_id=(None if target is None else target.revision_id),
                    content=decision.content,
                    category=decision.category,
                    confidence=decision.confidence,
                    change_reason=decision.change_reason,
                    decision_reason=decision.decision_reason,
                )
            )
        return tuple(writes)

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement:
        if claim.job_type != "memory_consolidate" or claim.scope.owner_user_id is None or not claim.namespace or claim.run_id is not None or claim.occurrence_id is not None:
            return JobOutcome.cancelled()
        await authority.heartbeat()
        if authority.cancel_requested:
            return JobOutcome.cancelled()
        try:
            allowed, work = await self._load_work(claim)
        except asyncio.CancelledError:
            raise
        except MemoryV2ConsolidationConflict:
            return JobOutcome.failed("MEMORY_CONSOLIDATE_WORK_INVALID")
        except Exception:
            return JobOutcome.failed("MEMORY_CONSOLIDATE_WORK_UNAVAILABLE")
        if not allowed:
            return JobOutcome.failed("MEMORY_CONSOLIDATE_SCOPE_UNAVAILABLE")
        if work is None:
            return JobOutcome.failed("MEMORY_CONSOLIDATE_WORK_UNAVAILABLE")
        if authority.cancel_requested or work.cancel_requested or work.suppressed:
            return self._settlement(
                claim,
                work,
                decisions=(),
                max_facts=10,
                confidence_threshold=0.0,
                cancel=True,
                release_candidates=False,
            )
        if work.fact_committed:
            return self._settlement(
                claim,
                work,
                decisions=(),
                max_facts=10,
                confidence_threshold=0.0,
                cancel=False,
                release_candidates=False,
            )
        if not self._is_supported_contract(work):
            return JobOutcome.failed("MEMORY_CONSOLIDATE_CONTRACT_UNSUPPORTED")

        try:
            current = await self._runtime_policy_materializer.materialize_current(
                RuntimePolicySection.AGENT_RUNTIME,
            )
            if not isinstance(current, AgentRuntimePolicyValue):
                return JobOutcome.failed("MEMORY_CONSOLIDATE_POLICY_INVALID")
            if not current.memory.enabled or current.memory.pipeline_mode not in {"consolidate", "v2"}:
                return self._settlement(
                    claim,
                    work,
                    decisions=(),
                    max_facts=10,
                    confidence_threshold=0.0,
                    cancel=True,
                    release_candidates=True,
                )
            frozen = await self._runtime_policy_materializer.materialize_revision(
                RuntimePolicySection.AGENT_RUNTIME,
                work.policy_revision,
            )
            if not isinstance(frozen, AgentRuntimePolicyValue) or not frozen.memory.enabled or frozen.memory.pipeline_mode not in {"consolidate", "v2"}:
                return JobOutcome.failed("MEMORY_CONSOLIDATE_POLICY_INVALID")
        except asyncio.CancelledError:
            raise
        except Exception:
            return JobOutcome.failed("MEMORY_CONSOLIDATE_POLICY_UNAVAILABLE")

        normal_candidates = tuple(candidate for candidate in work.candidates if candidate.sensitivity == "normal")
        model = None
        if normal_candidates:
            try:
                model = await self._model_materializer.materialize_exact(
                    model_config_id=work.model_config_id,
                    model_config_version_id=work.model_config_version_id,
                    payload_checksum=work.model_config_checksum,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return JobOutcome.failed("MEMORY_CONSOLIDATE_MODEL_UNAVAILABLE")

        await authority.heartbeat()
        if authority.cancel_requested:
            return self._settlement(
                claim,
                work,
                decisions=(),
                max_facts=frozen.memory.max_facts,
                confidence_threshold=frozen.memory.fact_confidence_threshold,
                cancel=True,
                release_candidates=False,
            )
        try:
            model_decisions = ()
            if normal_candidates:
                consolidator = self._consolidator_factory(model)
                result = await consolidator.consolidate(
                    tuple(
                        MemoryConsolidationCandidateInput(
                            id=candidate.id,
                            candidate_type=candidate.candidate_type,
                            content=candidate.content,
                            confidence=candidate.confidence,
                            retention_class=candidate.retention_class,
                        )
                        for candidate in normal_candidates
                    ),
                    tuple(
                        MemoryConsolidationFactInput(
                            id=fact.id,
                            revision_id=fact.revision_id,
                            fact_kind=fact.fact_kind,
                            content=fact.content,
                            category=fact.category,
                            confidence=fact.confidence,
                        )
                        for fact in work.facts[: frozen.memory.max_facts]
                    ),
                )
                model_decisions = result.decisions
            writes = self._writes(
                work,
                model_decisions,
                max_facts=frozen.memory.max_facts,
                confidence_threshold=frozen.memory.fact_confidence_threshold,
            )
        except asyncio.CancelledError:
            raise
        except MemoryConsolidationError as error:
            return JobOutcome.failed(error.code)
        except (TypeError, ValueError):
            return JobOutcome.failed("MEMORY_CONSOLIDATE_OUTPUT_INVALID")
        except Exception:
            return JobOutcome.failed("MEMORY_CONSOLIDATE_UNAVAILABLE")

        try:
            current = await self._runtime_policy_materializer.materialize_current(
                RuntimePolicySection.AGENT_RUNTIME,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return JobOutcome.failed("MEMORY_CONSOLIDATE_POLICY_UNAVAILABLE")
        if not isinstance(current, AgentRuntimePolicyValue):
            return JobOutcome.failed("MEMORY_CONSOLIDATE_POLICY_INVALID")
        if not current.memory.enabled or current.memory.pipeline_mode not in {
            "consolidate",
            "v2",
        }:
            return self._settlement(
                claim,
                work,
                decisions=(),
                max_facts=frozen.memory.max_facts,
                confidence_threshold=frozen.memory.fact_confidence_threshold,
                cancel=True,
                release_candidates=True,
            )

        await authority.heartbeat()
        if authority.cancel_requested:
            return self._settlement(
                claim,
                work,
                decisions=(),
                max_facts=frozen.memory.max_facts,
                confidence_threshold=frozen.memory.fact_confidence_threshold,
                cancel=True,
                release_candidates=False,
            )
        return self._settlement(
            claim,
            work,
            decisions=writes,
            max_facts=frozen.memory.max_facts,
            confidence_threshold=frozen.memory.fact_confidence_threshold,
            cancel=False,
            release_candidates=False,
        )

    def _settlement(
        self,
        claim: JobClaim,
        work: MemoryConsolidationWork,
        *,
        decisions: tuple[MemoryConsolidationDecisionWrite, ...],
        max_facts: int,
        confidence_threshold: float,
        cancel: bool,
        release_candidates: bool,
    ) -> JobSettlement:
        async def commit() -> None:
            async with self._sessions() as session, session.begin():
                allowed = await self._scope_validator(
                    session,
                    claim,
                    lock=True,
                )
                try:
                    async with session.begin_nested():
                        if not allowed:
                            raise MemoryV2ConsolidationConflict(
                                "Memory consolidation scope is unavailable",
                            )
                        try:
                            current = await self._runtime_policy_materializer.materialize_current_in_session(
                                session,
                                RuntimePolicySection.AGENT_RUNTIME,
                                for_update=True,
                            )
                        except Exception:
                            raise MemoryV2ConsolidationConflict(
                                "Memory consolidation policy is unavailable",
                            ) from None
                        if not isinstance(current, AgentRuntimePolicyValue):
                            raise MemoryV2ConsolidationConflict(
                                "Memory consolidation policy is invalid",
                            )
                        paused_at_commit = not current.memory.enabled or current.memory.pipeline_mode not in {
                            "consolidate",
                            "v2",
                        }
                        await self._repository(session).finalize_consolidation(
                            job_id=claim.job_id,
                            lease_token=claim.lease_token,
                            project_id=work.project_id,
                            owner_user_id=work.owner_user_id,
                            namespace=work.namespace,
                            generation_id=work.generation_id,
                            candidate_input_digest=work.candidate_input_digest,
                            contract_digest=work.contract_digest,
                            decisions=() if paused_at_commit else decisions,
                            max_facts=max_facts,
                            fact_confidence_threshold=confidence_threshold,
                            cancel=cancel or paused_at_commit,
                            release_candidates_on_cancel=(release_candidates or paused_at_commit),
                        )
                except MemoryV2ConsolidationLeaseLost:
                    raise LeaseLost(claim.job_id) from None
                except MemoryV2ConsolidationConflict:
                    changed = await self._job_repository_builder(session).retry_or_dead(
                        claim.job_id,
                        lease_token=claim.lease_token,
                        public_error_code="MEMORY_CONSOLIDATE_COMMIT_CONFLICT",
                        retry_initial_seconds=self._retry_initial_seconds,
                        retry_max_seconds=self._retry_max_seconds,
                    )
                    if not changed:
                        raise LeaseLost(claim.job_id) from None

        outcome = JobOutcome.cancelled() if cancel else JobOutcome.succeeded()
        return JobSettlement(outcome, commit)


class MemoryRetentionPurgeJobHandler:
    """Erase terminal Candidate bodies without calling a model."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        runtime_policy_materializer: SystemRuntimePolicyMaterializer | None = None,
        repository_builder=MemoryV2Repository,
        job_repository_builder=JobRepository,
        scope_validator: MemoryScopeValidator | None = None,
        retry_initial_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> None:
        if (
            not callable(session_factory)
            or not callable(repository_builder)
            or not callable(job_repository_builder)
            or type(retry_initial_seconds) is not int
            or type(retry_max_seconds) is not int
            or retry_initial_seconds < 1
            or retry_max_seconds < retry_initial_seconds
        ):
            raise ValueError("Memory retention Worker configuration is invalid")
        self._sessions = session_factory
        self._runtime_policy_materializer = runtime_policy_materializer or SystemRuntimePolicyMaterializer(session_factory)
        self._repository_builder = repository_builder
        self._job_repository_builder = job_repository_builder
        self._scope_validator = scope_validator or _default_retention_scope_validator
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds

    def _repository(self, session: AsyncSession) -> MemoryV2Repository:
        return self._repository_builder(
            session,
            jobs=self._job_repository_builder(session),
        )

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement:
        if claim.job_type != "memory_retention_purge" or claim.scope.owner_user_id is None or not claim.namespace or claim.run_id is not None or claim.occurrence_id is not None:
            return JobOutcome.cancelled()
        await authority.heartbeat()
        try:
            policy = await self._runtime_policy_materializer.materialize_current(
                RuntimePolicySection.AGENT_RUNTIME,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return JobOutcome.failed("MEMORY_RETENTION_POLICY_UNAVAILABLE")
        if not isinstance(policy, AgentRuntimePolicyValue):
            return JobOutcome.failed("MEMORY_RETENTION_POLICY_INVALID")
        cancel = authority.cancel_requested or not policy.memory.enabled or policy.memory.pipeline_mode not in {"consolidate", "v2"}

        if not cancel:
            try:
                policy = await self._runtime_policy_materializer.materialize_current(
                    RuntimePolicySection.AGENT_RUNTIME,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return JobOutcome.failed("MEMORY_RETENTION_POLICY_UNAVAILABLE")
            if not isinstance(policy, AgentRuntimePolicyValue):
                return JobOutcome.failed("MEMORY_RETENTION_POLICY_INVALID")
            cancel = not policy.memory.enabled or policy.memory.pipeline_mode not in {
                "consolidate",
                "v2",
            }

        async def commit() -> None:
            async with self._sessions() as session, session.begin():
                allowed = await self._scope_validator(
                    session,
                    claim,
                    lock=True,
                )
                try:
                    async with session.begin_nested():
                        try:
                            current = await self._runtime_policy_materializer.materialize_current_in_session(
                                session,
                                RuntimePolicySection.AGENT_RUNTIME,
                                for_update=True,
                            )
                        except Exception:
                            raise MemoryV2RetentionConflict(
                                "Memory retention policy is unavailable",
                            ) from None
                        if not isinstance(current, AgentRuntimePolicyValue):
                            raise MemoryV2RetentionConflict(
                                "Memory retention policy is invalid",
                            )
                        paused_at_commit = not current.memory.enabled or current.memory.pipeline_mode not in {
                            "consolidate",
                            "v2",
                        }
                        await self._repository(session).finalize_retention(
                            job_id=claim.job_id,
                            lease_token=claim.lease_token,
                            project_id=claim.scope.project_id,
                            owner_user_id=claim.scope.owner_user_id or "",
                            namespace=claim.namespace or "",
                            cancel=cancel or not allowed or paused_at_commit,
                        )
                except MemoryV2RetentionLeaseLost:
                    raise LeaseLost(claim.job_id) from None
                except MemoryV2RetentionConflict:
                    changed = await self._job_repository_builder(
                        session,
                    ).retry_or_dead(
                        claim.job_id,
                        lease_token=claim.lease_token,
                        public_error_code="MEMORY_RETENTION_COMMIT_CONFLICT",
                        retry_initial_seconds=self._retry_initial_seconds,
                        retry_max_seconds=self._retry_max_seconds,
                    )
                    if not changed:
                        raise LeaseLost(claim.job_id) from None

        outcome = JobOutcome.cancelled() if cancel else JobOutcome.succeeded()
        return JobSettlement(outcome, commit)


__all__ = [
    "MemoryConsolidateJobHandler",
    "MemoryRetentionPurgeJobHandler",
]
