"""Worker-only Dream execution over one frozen document/history batch."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountMemoryPreference,
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
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
from deerflow.agents.memory.dream import (
    DreamHistoryInput,
    MemoryDreamError,
    MemoryDreamInput,
    MemoryDreamResult,
    MemoryDreamRunner,
)
from deerflow.config.app_config import AppConfig
from deerflow.memory_contract import (
    DREAM_PROMPT_VERSION,
    MemoryDocumentInvalid,
    validate_memory_document,
)
from deerflow.models import ModelRuntime, ModelRuntimeProfile, model_supports_temperature
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.private_work.memory_document_repository import (
    BUDGET_REWRITE_HISTORY_DIGEST,
    MemoryDocumentConflict,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamLeaseConflict,
    MemoryDreamReleaseResult,
    MemoryDreamSettlementInvariant,
    MemoryDreamStaleConflict,
    MemoryDreamWork,
    compute_dream_history_digest,
    memory_document_deletion_ratio,
    memory_document_digest,
)

_MEMORY_DREAM_REQUEST_ID = "memory-dream-worker"
logger = logging.getLogger(__name__)


class _DreamSettlementTransient(RuntimeError):
    """Content-free transient signal handled after its transaction rolls back."""

    def __init__(self, public_error_code: str) -> None:
        self.public_error_code = public_error_code
        super().__init__("Dream settlement is temporarily unavailable")


def _deletion_ratio_bucket(ratio: float) -> str:
    """Content-free decile bucket for the review audit metadata."""

    lower = min(90, int(ratio * 10) * 10)
    return f"{lower}-{lower + 10}%"


class DreamRunnerPort(Protocol):
    async def run(self, value: MemoryDreamInput) -> MemoryDreamResult: ...


class MemoryDreamScopeValidator(Protocol):
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
            _MEMORY_DREAM_REQUEST_ID,
            lock=lock,
        )
        context.require(Capability.PRIVATE_WORK_CREATE)
        return True
    except (ProjectForbidden, ProjectNotFound, ValueError):
        return False


class MemoryDreamJobHandler:
    """Run a restricted Dream model and defer every write to JobSettlement."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        app_config: AppConfig | None,
        model_materializer: SystemModelMaterializer | None = None,
        runtime_policy_materializer: SystemRuntimePolicyMaterializer | None = None,
        runner_factory: Callable[[object], DreamRunnerPort] | None = None,
        repository_builder=MemoryDocumentRepository,
        job_repository_builder=JobRepository,
        scope_validator: MemoryDreamScopeValidator | None = None,
        personalization_repository_builder=AccountPersonalizationRepository,
        retry_initial_seconds: int = 5,
        retry_max_seconds: int = 300,
        audit=None,
    ) -> None:
        if (
            not callable(session_factory)
            or not callable(repository_builder)
            or not callable(job_repository_builder)
            or not callable(personalization_repository_builder)
            or (runner_factory is None and not isinstance(app_config, AppConfig))
            or type(retry_initial_seconds) is not int
            or type(retry_max_seconds) is not int
            or retry_initial_seconds < 1
            or retry_max_seconds < retry_initial_seconds
        ):
            raise ValueError("Dream Worker configuration is invalid")
        if audit is not None and not all(
            callable(getattr(audit, method, None))
            for method in (
                "memory_dream_review_flagged",
                "memory_dream_settled",
            )
        ):
            raise ValueError("Dream Worker audit port is invalid")
        self._sessions = session_factory
        self._app_config = app_config
        self._model_materializer = model_materializer or SystemModelMaterializer(session_factory)
        self._runtime_policy_materializer = runtime_policy_materializer or SystemRuntimePolicyMaterializer(session_factory)
        self._runner_factory = runner_factory or self._make_runner
        self._repository_builder = repository_builder
        self._job_repository_builder = job_repository_builder
        self._scope_validator = scope_validator or _default_scope_validator
        self._personalization_repository_builder = personalization_repository_builder
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds
        self._audit = audit

    def _repository(self, session: AsyncSession) -> MemoryDocumentRepository:
        return self._repository_builder(
            session,
            jobs=self._job_repository_builder(session),
        )

    def _make_runner(self, model: object) -> MemoryDreamRunner:
        if self._app_config is None:
            raise RuntimeError("Dream app config is unavailable")
        model_name = getattr(model, "name", None)
        if not isinstance(model_name, str) or not model_name:
            raise RuntimeError("Dream model is invalid")
        runtime_config = self._app_config.with_runtime_models((model,))
        overrides = {"temperature": 0.0} if model_supports_temperature(model_name, app_config=runtime_config) else None
        model_runtime = ModelRuntime(app_config=runtime_config)
        chat_model = model_runtime.build_chat_model(
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
            model_name=model_name,
            model_overrides=overrides,
        )
        return MemoryDreamRunner(
            chat_model,
            model_runtime=model_runtime,
        )

    @staticmethod
    def _scope(claim: JobClaim) -> MemoryDocumentScope:
        return MemoryDocumentScope(
            project_id=claim.scope.project_id,
            owner_user_id=claim.scope.owner_user_id or "",
            namespace=claim.namespace or "",
        )

    async def _load(
        self,
        claim: JobClaim,
    ) -> tuple[
        bool,
        AccountMemoryPreference | None,
        MemoryDreamWork | None,
    ]:
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
            work = await self._repository(session).load_dream_work(
                self._scope(claim),
                claim.job_id,
            )
        return True, preference, work

    @staticmethod
    def _input(
        work: MemoryDreamWork,
        *,
        max_tokens: int,
    ) -> MemoryDreamInput:
        if (
            work.prompt_version != DREAM_PROMPT_VERSION
            or work.result_version is not None
            or work.history_count != len(work.history)
            or memory_document_digest(work.base_content) != work.base_content_digest
            or not isinstance(work.sections_policy_version_id, uuid.UUID)
        ):
            raise ValueError("Dream frozen work is invalid")
        if work.trigger == "budget_rewrite":
            if work.history or work.history_from is not None or work.history_to is not None or work.history_digest != BUDGET_REWRITE_HISTORY_DIGEST:
                raise ValueError("Dream frozen work is invalid")
        elif (
            not work.history
            or work.history_from != work.history[0].sequence
            or work.history_to != work.history[-1].sequence
            or compute_dream_history_digest(work.history) != work.history_digest
            or any(item.tagged_text is None for item in work.history)
        ):
            raise ValueError("Dream frozen work is invalid")
        return MemoryDreamInput(
            document=work.base_content,
            document_version=work.base_document_version,
            history=tuple(
                DreamHistoryInput(
                    sequence=item.sequence,
                    tagged_text=item.tagged_text or "",
                    origin=item.origin,
                )
                for item in work.history
            ),
            max_tokens=max_tokens,
            sections=work.sections,
            budget_rewrite=work.trigger == "budget_rewrite",
        )

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement:
        if claim.job_type != "memory_dream" or claim.scope.owner_user_id is None or not claim.namespace or claim.run_id is not None or claim.occurrence_id is not None:
            return JobOutcome.cancelled()
        await authority.heartbeat()
        try:
            allowed, preference, work = await self._load(claim)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._release_settlement(
                claim,
                cancelled=False,
                public_error_code="MEMORY_DREAM_WORK_UNAVAILABLE",
            )
        if not allowed or preference is None or work is None:
            return self._release_settlement(
                claim,
                cancelled=not allowed,
                public_error_code="MEMORY_DREAM_WORK_UNAVAILABLE",
            )
        if authority.cancel_requested or work.cancel_requested or not preference.memory_enabled or preference.version != work.preference_version:
            return self._release_settlement(claim, cancelled=True)
        if work.prompt_version != DREAM_PROMPT_VERSION:
            # Prompt text is frozen admission authority.  A deployed prompt
            # version change makes this batch stale rather than transiently
            # unavailable, so release it as cancelled without materializing a
            # model or retrying the same permanently incompatible work.
            return self._release_settlement(
                claim,
                cancelled=True,
                retryable=False,
            )
        try:
            frozen_policy = await self._runtime_policy_materializer.materialize_revision(
                RuntimePolicySection.AGENT_RUNTIME,
                work.policy_revision,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._release_settlement(
                claim,
                cancelled=False,
                public_error_code="MEMORY_DREAM_POLICY_UNAVAILABLE",
            )
        if not isinstance(frozen_policy, AgentRuntimePolicyValue) or not frozen_policy.memory.enabled:
            return self._release_settlement(claim, cancelled=True)
        try:
            model = await self._model_materializer.materialize_frozen(
                work.model_execution,
            )
            dream_input = self._input(
                work,
                max_tokens=frozen_policy.memory.max_injection_tokens,
            )
        except asyncio.CancelledError:
            raise
        except (MemoryDocumentInvalid, TypeError, ValueError):
            return self._release_settlement(
                claim,
                cancelled=False,
                public_error_code="MEMORY_DREAM_WORK_INVALID",
            )
        except Exception:
            return self._release_settlement(
                claim,
                cancelled=False,
                public_error_code="MEMORY_DREAM_MODEL_UNAVAILABLE",
            )

        await authority.heartbeat()
        if authority.cancel_requested:
            return self._release_settlement(claim, cancelled=True)
        try:
            result = await self._runner_factory(model).run(dream_input)
            if type(result) is not MemoryDreamResult or result.replaced is not True:
                raise MemoryDreamError(
                    "MEMORY_DREAM_REPLACEMENT_REQUIRED",
                )
            validate_memory_document(
                result.content,
                frozen_policy.memory.max_injection_tokens,
                sections=work.sections,
            )
        except asyncio.CancelledError:
            raise
        except MemoryDreamError as error:
            return self._release_settlement(
                claim,
                cancelled=False,
                public_error_code=error.code,
            )
        except (MemoryDocumentInvalid, TypeError, ValueError):
            return self._release_settlement(
                claim,
                cancelled=False,
                public_error_code="MEMORY_DREAM_OUTPUT_INVALID",
            )
        except Exception:
            return self._release_settlement(
                claim,
                cancelled=False,
                public_error_code="MEMORY_DREAM_MODEL_FAILED",
            )
        await authority.heartbeat()
        if authority.cancel_requested:
            return self._release_settlement(claim, cancelled=True)
        return self._success_settlement(
            claim,
            work=work,
            content=result.content,
            max_tokens=frozen_policy.memory.max_injection_tokens,
            episode_retention_days=frozen_policy.memory.episode_retention_days,
        )

    def _success_settlement(
        self,
        claim: JobClaim,
        *,
        work: MemoryDreamWork,
        content: str,
        max_tokens: int,
        episode_retention_days: int,
    ) -> JobSettlement:
        async def commit() -> None:
            retry_error_code: str | None = None
            retryable = True
            cancelled = False
            try:
                async with self._sessions() as session, session.begin():
                    try:
                        allowed = await self._scope_validator(
                            session,
                            claim,
                            lock=True,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise _DreamSettlementTransient("MEMORY_DREAM_SETTLEMENT_UNAVAILABLE") from None
                    repository = self._repository(session)
                    if not allowed:
                        try:
                            result = await repository.release_dream(
                                self._scope(claim),
                                job_id=claim.job_id,
                                lease_token=claim.lease_token,
                                now=datetime.now(UTC),
                                cancelled=True,
                            )
                        except MemoryDreamLeaseConflict:
                            raise LeaseLost(claim.job_id) from None
                        except MemoryDocumentConflict:
                            raise
                        except Exception:
                            raise _DreamSettlementTransient("MEMORY_DREAM_SETTLEMENT_UNAVAILABLE") from None
                        result = self._require_release_result(result)
                        await self._audit_release_result(
                            session,
                            claim,
                            result,
                            public_error_code="MEMORY_DREAM_CANCELLED",
                        )
                        return

                    try:
                        current, policy_revision = await self._runtime_policy_materializer.materialize_current_with_revision_in_session(
                            session,
                            RuntimePolicySection.AGENT_RUNTIME,
                            for_update=True,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise _DreamSettlementTransient("MEMORY_DREAM_POLICY_UNAVAILABLE") from None
                    if not isinstance(current, AgentRuntimePolicyValue):
                        raise _DreamSettlementTransient("MEMORY_DREAM_POLICY_UNAVAILABLE")

                    try:
                        preference = await self._personalization_repository_builder(session).read_memory(
                            work.owner_user_id,
                            for_update=True,
                        )
                    except AccountPersonalizationNotFound:
                        preference = None
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise _DreamSettlementTransient("MEMORY_DREAM_SETTLEMENT_UNAVAILABLE") from None

                    still_valid = (
                        preference is not None
                        and preference.memory_enabled
                        and preference.version == work.preference_version
                        and current.memory.enabled
                        and policy_revision == work.policy_revision
                        and current.memory.max_injection_tokens == max_tokens
                    )
                    if not still_valid:
                        try:
                            result = await repository.release_dream(
                                self._scope(claim),
                                job_id=claim.job_id,
                                lease_token=claim.lease_token,
                                now=datetime.now(UTC),
                                cancelled=True,
                            )
                        except MemoryDreamLeaseConflict:
                            raise LeaseLost(claim.job_id) from None
                        except MemoryDocumentConflict:
                            raise
                        except Exception:
                            raise _DreamSettlementTransient("MEMORY_DREAM_SETTLEMENT_UNAVAILABLE") from None
                        result = self._require_release_result(result)
                        await self._audit_release_result(
                            session,
                            claim,
                            result,
                            public_error_code="MEMORY_DREAM_CANCELLED",
                        )
                        return

                    try:
                        validate_memory_document(
                            content,
                            max_tokens,
                            sections=work.sections,
                        )
                    except (MemoryDocumentInvalid, TypeError, ValueError):
                        raise _DreamSettlementTransient("MEMORY_DREAM_OUTPUT_INVALID") from None

                    try:
                        record = await repository.finalize_dream(
                            self._scope(claim),
                            job_id=claim.job_id,
                            lease_token=claim.lease_token,
                            expected_history_digest=work.history_digest,
                            expected_base_version=work.base_document_version,
                            expected_base_digest=work.base_content_digest,
                            expected_sections=work.sections,
                            content=content,
                            now=datetime.now(UTC),
                            episode_retention_days=episode_retention_days,
                        )
                    except MemoryDreamLeaseConflict:
                        raise LeaseLost(claim.job_id) from None
                    except MemoryDreamStaleConflict:
                        raise
                    except MemoryDreamSettlementInvariant:
                        raise
                    except MemoryDocumentConflict:
                        raise MemoryDreamSettlementInvariant(
                            "Dream settlement conflict was not classified",
                        ) from None
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise _DreamSettlementTransient("MEMORY_DREAM_SETTLEMENT_UNAVAILABLE") from None

                    if self._audit is not None:
                        try:
                            await self._audit.memory_dream_settled(
                                session,
                                project_id=work.project_id,
                                job_id=claim.job_id,
                                request_id=_MEMORY_DREAM_REQUEST_ID,
                                disposition="published",
                                version=record.version,
                            )
                            if record.needs_review:
                                ratio = memory_document_deletion_ratio(
                                    work.base_content,
                                    content,
                                )
                                await self._audit.memory_dream_review_flagged(
                                    session,
                                    project_id=work.project_id,
                                    job_id=claim.job_id,
                                    request_id=_MEMORY_DREAM_REQUEST_ID,
                                    version=record.version,
                                    deletion_ratio_bucket=_deletion_ratio_bucket(
                                        ratio or 0.0,
                                    ),
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            raise _DreamSettlementTransient("MEMORY_DREAM_AUDIT_UNAVAILABLE") from None
            except asyncio.CancelledError:
                raise
            except _DreamSettlementTransient as error:
                retry_error_code = error.public_error_code
            except MemoryDreamStaleConflict:
                retry_error_code = "MEMORY_DREAM_STALE"
                cancelled = True
                retryable = False
            except MemoryDreamSettlementInvariant:
                retry_error_code = "MEMORY_DREAM_SETTLEMENT_INVARIANT"
                retryable = False
            except LeaseLost:
                raise
            except MemoryDocumentConflict:
                retry_error_code = "MEMORY_DREAM_SETTLEMENT_INVARIANT"
                retryable = False
            except Exception:
                # Includes transaction start/commit failures. The subsequent
                # release uses a fresh session only after this context has
                # completed its rollback/close path.
                retry_error_code = "MEMORY_DREAM_SETTLEMENT_UNAVAILABLE"

            if retry_error_code is not None:
                logger.warning(
                    "Memory Dream settlement rolled back; entering retry/dead: public_error_code=%s",
                    retry_error_code,
                )
                await self._commit_release(
                    claim,
                    cancelled=cancelled,
                    public_error_code=retry_error_code,
                    retryable=retryable,
                )

        return JobSettlement(JobOutcome.succeeded(), commit)

    @staticmethod
    def _require_release_result(value: object) -> MemoryDreamReleaseResult:
        if type(value) is not MemoryDreamReleaseResult:
            raise MemoryDreamSettlementInvariant(
                "Dream release returned an invalid result",
            )
        return value

    async def _audit_release_result(
        self,
        session: AsyncSession,
        claim: JobClaim,
        result: MemoryDreamReleaseResult,
        *,
        public_error_code: str,
    ) -> None:
        """Audit only durable terminal releases in their owning transaction."""

        if self._audit is None or result.disposition in {
            "already_published",
            "retry_wait",
        }:
            return
        if result.disposition == "cancelled":
            metadata = {
                "disposition": "cancelled",
                "version": None,
                "public_error_code": None,
            }
        elif result.disposition == "dead":
            metadata = {
                "disposition": "dead",
                "version": None,
                "public_error_code": public_error_code,
            }
        else:
            raise MemoryDreamSettlementInvariant(
                "Dream release returned an invalid terminal result",
            )
        try:
            await self._audit.memory_dream_settled(
                session,
                project_id=claim.scope.project_id,
                job_id=claim.job_id,
                request_id=_MEMORY_DREAM_REQUEST_ID,
                **metadata,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _DreamSettlementTransient(
                "MEMORY_DREAM_AUDIT_UNAVAILABLE",
            ) from None

    async def _commit_release_once(
        self,
        claim: JobClaim,
        *,
        cancelled: bool,
        public_error_code: str,
        retryable: bool,
    ) -> MemoryDreamReleaseResult:
        async with self._sessions() as session, session.begin():
            try:
                allowed = await self._scope_validator(
                    session,
                    claim,
                    lock=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise _DreamSettlementTransient(
                    "MEMORY_DREAM_SETTLEMENT_UNAVAILABLE",
                ) from None
            try:
                result = await self._repository(session).release_dream(
                    self._scope(claim),
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    now=datetime.now(UTC),
                    cancelled=cancelled or not allowed,
                    public_error_code=public_error_code,
                    retryable=retryable,
                    retry_initial_seconds=self._retry_initial_seconds,
                    retry_max_seconds=self._retry_max_seconds,
                )
            except MemoryDreamLeaseConflict:
                raise LeaseLost(claim.job_id) from None
            result = self._require_release_result(result)
            await self._audit_release_result(
                session,
                claim,
                result,
                public_error_code=public_error_code,
            )
            return result

    async def _commit_release(
        self,
        claim: JobClaim,
        *,
        cancelled: bool,
        public_error_code: str,
        retryable: bool,
    ) -> MemoryDreamReleaseResult:
        try:
            return await self._commit_release_once(
                claim,
                cancelled=cancelled,
                public_error_code=public_error_code,
                retryable=retryable,
            )
        except MemoryDreamSettlementInvariant:
            if not retryable:
                raise
            # The first release transaction has rolled back.  A fresh,
            # non-retryable release makes an impossible settlement state
            # terminal instead of misreporting lease loss or waiting for lease
            # expiry.  If the invariant repeats, preserve it for operators.
            logger.error("Memory Dream release invariant; forcing dead settlement")
            return await self._commit_release_once(
                claim,
                cancelled=False,
                public_error_code="MEMORY_DREAM_SETTLEMENT_INVARIANT",
                retryable=False,
            )

    def _release_settlement(
        self,
        claim: JobClaim,
        *,
        cancelled: bool,
        public_error_code: str = "MEMORY_DREAM_CANCELLED",
        retryable: bool = True,
    ) -> JobSettlement:
        async def commit() -> None:
            await self._commit_release(
                claim,
                cancelled=cancelled,
                public_error_code=public_error_code,
                retryable=retryable,
            )

        outcome = JobOutcome.cancelled() if cancelled else JobOutcome.failed(public_error_code)
        return JobSettlement(outcome, commit)


__all__ = ["MemoryDreamJobHandler"]
