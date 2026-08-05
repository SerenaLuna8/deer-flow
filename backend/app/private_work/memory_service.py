from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.memory_source_admission import SourceHmacRef
from app.private_work.memory_v2_export import iter_memory_v2_export_records
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_settings.repository import SystemModelRepository
from deerflow.agents.memory.consolidator import (
    MEMORY_CONSOLIDATE_OUTPUT_SCHEMA_VERSION,
    MEMORY_CONSOLIDATE_PROMPT_VERSION,
    MEMORY_CONSOLIDATOR_VERSION,
)
from deerflow.agents.memory.storage import ProjectMemorySnapshot, ProjectMemoryStorage
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryInvalid as RepositoryMemoryInvalid,
)
from deerflow.persistence.private_work.memory_v2_management import (
    MemoryCandidateStatus,
    MemoryV2CandidateView,
    MemoryV2FactDetail,
    MemoryV2FactView,
    MemoryV2HardForgetResult,
    MemoryV2ManagementConflict,
    MemoryV2ManagementInvalid,
    MemoryV2ManagementNotFound,
    MemoryV2ManagementRepository,
)
from deerflow.persistence.private_work.memory_v2_repository import (
    MemoryConsolidationAdmissionContract,
    MemoryConsolidationImmediateAdmission,
    MemoryV2AdmissionConflict,
    MemoryV2Repository,
)

_FACT_LINEAGE_HMAC_DOMAIN = b"deerflow.memory.fact-lineage.v1\x00"


class MemoryV2AuditPort(Protocol):
    async def memory_changed(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        resource_id: uuid.UUID,
        operation: str,
        affected_count: int,
    ) -> None: ...


MemorySourceHmac = Callable[[bytes], SourceHmacRef]


@dataclass(frozen=True, slots=True)
class PrivateMemoryStatus:
    namespace: str
    version: int
    fact_count: int
    last_updated: str


@dataclass(frozen=True, slots=True)
class MemoryConsolidationRuntime:
    enabled: bool
    pipeline_mode: Literal["off", "shadow", "consolidate", "v2"]
    consolidation_interval_minutes: int
    candidate_retention_days: int
    fact_confidence_threshold: float
    max_facts: int
    policy_revision: int
    model_config_id: uuid.UUID | None
    model_config_version_id: uuid.UUID | None
    model_config_checksum: str | None


MemoryRuntimeResolver = Callable[
    [AsyncSession],
    Awaitable[MemoryConsolidationRuntime],
]


async def resolve_memory_consolidation_runtime(
    session: AsyncSession,
) -> MemoryConsolidationRuntime:
    locked = await SystemRuntimePolicyService.read_agent_runtime_for_admission(
        session,
    )
    memory = locked.value.memory
    model_config_id = None
    model_config_version_id = None
    model_config_checksum = None
    if memory.enabled and memory.pipeline_mode in {"consolidate", "v2"}:
        material = await SystemModelRepository(session).resolve_active_model(
            memory.model_name,
            load_envelope=False,
        )
        if material is not None:
            model_config_id = material.model.id
            model_config_version_id = material.version.id
            model_config_checksum = material.version.payload_checksum
    return MemoryConsolidationRuntime(
        enabled=memory.enabled,
        pipeline_mode=memory.pipeline_mode,
        consolidation_interval_minutes=memory.consolidation_interval_minutes,
        candidate_retention_days=memory.candidate_retention_days,
        fact_confidence_threshold=memory.fact_confidence_threshold,
        max_facts=memory.max_facts,
        policy_revision=locked.revision,
        model_config_id=model_config_id,
        model_config_version_id=model_config_version_id,
        model_config_checksum=model_config_checksum,
    )


def build_memory_consolidation_contract(
    runtime: MemoryConsolidationRuntime,
) -> MemoryConsolidationAdmissionContract | None:
    if runtime.model_config_id is None or runtime.model_config_version_id is None or runtime.model_config_checksum is None:
        return None
    return MemoryConsolidationAdmissionContract(
        interval_minutes=runtime.consolidation_interval_minutes,
        policy_revision=runtime.policy_revision,
        model_config_id=runtime.model_config_id,
        model_config_version_id=runtime.model_config_version_id,
        model_config_checksum=runtime.model_config_checksum,
        prompt_version=MEMORY_CONSOLIDATE_PROMPT_VERSION,
        consolidator_version=MEMORY_CONSOLIDATOR_VERSION,
        output_schema_version=MEMORY_CONSOLIDATE_OUTPUT_SCHEMA_VERSION,
    )


class PrivateMemoryService:
    """Application boundary for callable project Memory operations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: ProjectMemoryStorage | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage or ProjectMemoryStorage(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()

    async def _require(
        self,
        context: PrivateWorkContext,
        capability: Capability,
    ) -> PrivateWorkContext:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, capability)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        return context

    @staticmethod
    def _map_storage_error(context: PrivateWorkContext, exc: Exception) -> PrivateWorkError:
        if isinstance(exc, (RepositoryMemoryInvalid, ValueError, IntegrityError)):
            return PrivateWorkInvalid(context.request_id)
        return PrivateWorkUnavailable(context.request_id)

    async def _read(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        context = await self._require(context, Capability.PRIVATE_WORK_READ_OWN)
        try:
            return await self._storage.load(
                scope=context.resource_scope,
                namespace=namespace,
            )
        except Exception as exc:
            raise self._map_storage_error(context, exc) from None

    async def status(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
    ) -> PrivateMemoryStatus:
        snapshot = await self._read(context, namespace=namespace)
        return PrivateMemoryStatus(
            namespace=namespace,
            version=snapshot.version,
            fact_count=len(snapshot.memory.get("facts", [])),
            last_updated=str(snapshot.memory.get("lastUpdated", "")),
        )

    async def list(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
    ) -> ProjectMemorySnapshot:
        return await self._read(context, namespace=namespace)

    async def export(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
    ) -> dict:
        return copy.deepcopy((await self._read(context, namespace=namespace)).memory)


class PrivateMemoryV2Service:
    """Transactional application boundary for Memory v2 management."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        source_hmac: MemorySourceHmac,
        audit: MemoryV2AuditPort | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
        repository_builder=MemoryV2ManagementRepository,
        consolidation_repository_builder=MemoryV2Repository,
        runtime_resolver: MemoryRuntimeResolver = resolve_memory_consolidation_runtime,
    ) -> None:
        if not callable(session_factory) or not callable(source_hmac) or not callable(repository_builder) or not callable(consolidation_repository_builder) or not callable(runtime_resolver):
            raise ValueError("Memory v2 service configuration is invalid")
        self._session_factory = session_factory
        self._source_hmac = source_hmac
        self._audit = audit
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._repository_builder = repository_builder
        self._consolidation_repository_builder = consolidation_repository_builder
        self._runtime_resolver = runtime_resolver

    @staticmethod
    def _map_error(
        context: PrivateWorkContext,
        error: Exception,
    ) -> PrivateWorkError:
        if isinstance(error, MemoryV2ManagementNotFound):
            return PrivateWorkNotFound(context.request_id)
        if isinstance(error, (MemoryV2ManagementConflict, MemoryV2AdmissionConflict)):
            return PrivateWorkConflict(context.request_id)
        if isinstance(error, (MemoryV2ManagementInvalid, ValueError, IntegrityError)):
            return PrivateWorkInvalid(context.request_id)
        return PrivateWorkUnavailable(context.request_id)

    @staticmethod
    def _context(context: PrivateWorkContext) -> PrivateWorkContext:
        return require_issued_private_work_context(context)

    async def _audit_change(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        resource_id: uuid.UUID,
        operation: str,
        affected_count: int = 1,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.memory_changed(
            session,
            context,
            resource_id=resource_id,
            operation=operation,
            affected_count=affected_count,
        )

    async def list_facts(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str,
        statuses: tuple[Literal["active", "disabled"], ...],
        limit: int,
        offset: int,
        query: str | None = None,
        category: str | None = None,
    ) -> tuple[MemoryV2FactView, ...]:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).list_facts(
                    context.resource_scope,
                    namespace=namespace,
                    statuses=statuses,
                    limit=limit,
                    offset=offset,
                    query=query,
                    category=category,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    async def list_candidates(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str,
        statuses: tuple[MemoryCandidateStatus, ...],
        limit: int,
        offset: int,
    ) -> tuple[MemoryV2CandidateView, ...]:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).list_candidates(
                    context.resource_scope,
                    namespace=namespace,
                    statuses=statuses,
                    limit=limit,
                    offset=offset,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    async def get_fact(
        self,
        context: PrivateWorkContext,
        fact_id: uuid.UUID,
        *,
        namespace: str,
    ) -> MemoryV2FactDetail:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).get_fact_detail(
                    context.resource_scope,
                    namespace=namespace,
                    fact_id=fact_id,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    async def open_export(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str,
    ) -> AsyncIterator[bytes]:
        context = self._context(context)
        if not isinstance(namespace, str) or namespace != namespace.strip() or not 1 <= len(namespace) <= 128:
            raise PrivateWorkInvalid(context.request_id)
        session = self._session_factory()
        transaction = await session.begin()
        try:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await self._revalidator.require(
                session,
                context,
                Capability.PRIVATE_WORK_READ_OWN,
            )
        except PrivateWorkError:
            await transaction.rollback()
            await session.close()
            raise
        except DBAPIError:
            await transaction.rollback()
            await session.close()
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            await transaction.rollback()
            await session.close()
            raise self._map_error(context, error) from None

        async def stream() -> AsyncIterator[bytes]:
            try:
                yield (
                    json.dumps(
                        {
                            "record_type": "manifest",
                            "schema_version": 2,
                            "format": "deer-flow-memory-v2-ndjson",
                            "generated_at": datetime.now(UTC).isoformat(),
                            "project_id": str(context.project_id),
                            "namespace": namespace,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                async for record_type, data in iter_memory_v2_export_records(
                    session,
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    namespace=namespace,
                ):
                    yield (
                        json.dumps(
                            {"record_type": record_type, "data": data},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
            finally:
                if transaction.is_active:
                    await transaction.rollback()
                await session.close()

        return stream()

    async def consolidate_now(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str,
    ) -> MemoryConsolidationImmediateAdmission:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                await self._revalidator.require(
                    session,
                    context,
                    Capability.SHARED_ASSETS_EXECUTE,
                )
                runtime = await self._runtime_resolver(session)
                if not runtime.enabled or runtime.pipeline_mode not in {
                    "consolidate",
                    "v2",
                }:
                    raise PrivateWorkConflict(context.request_id)
                contract = build_memory_consolidation_contract(runtime)
                if contract is None:
                    raise PrivateWorkUnavailable(context.request_id)
                return await self._consolidation_repository_builder(session).admit_consolidation_for_scope(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    namespace=namespace,
                    contract=contract,
                    now=datetime.now(UTC),
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    async def accept_candidate(
        self,
        context: PrivateWorkContext,
        candidate_id: uuid.UUID,
        *,
        namespace: str,
        expected_updated_at: datetime,
    ) -> MemoryV2FactView:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                result = await self._repository_builder(session).accept_candidate(
                    context.resource_scope,
                    namespace=namespace,
                    candidate_id=candidate_id,
                    expected_updated_at=expected_updated_at,
                    now=datetime.now(UTC),
                )
                await self._audit_change(
                    session,
                    context,
                    resource_id=candidate_id,
                    operation="candidate_accept",
                )
                return result
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    async def reject_candidate(
        self,
        context: PrivateWorkContext,
        candidate_id: uuid.UUID,
        *,
        namespace: str,
        expected_updated_at: datetime,
    ) -> MemoryV2CandidateView:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                result = await self._repository_builder(session).reject_candidate(
                    context.resource_scope,
                    namespace=namespace,
                    candidate_id=candidate_id,
                    expected_updated_at=expected_updated_at,
                    now=datetime.now(UTC),
                )
                await self._audit_change(
                    session,
                    context,
                    resource_id=candidate_id,
                    operation="candidate_reject",
                )
                return result
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    async def revise_fact(
        self,
        context: PrivateWorkContext,
        fact_id: uuid.UUID,
        *,
        namespace: str,
        expected_version: int,
        content: str | None,
        category: str | None,
        confidence: float | None,
        reason: str | None,
    ) -> MemoryV2FactView:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                result = await self._repository_builder(session).revise_fact(
                    context.resource_scope,
                    namespace=namespace,
                    fact_id=fact_id,
                    expected_version=expected_version,
                    content=content,
                    category=category,
                    confidence=confidence,
                    reason=reason,
                    now=datetime.now(UTC),
                )
                await self._audit_change(
                    session,
                    context,
                    resource_id=fact_id,
                    operation="fact_edit",
                )
                return result
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    async def set_fact_enabled(
        self,
        context: PrivateWorkContext,
        fact_id: uuid.UUID,
        *,
        namespace: str,
        expected_version: int,
        enabled: bool,
    ) -> MemoryV2FactView:
        context = self._context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                result = await self._repository_builder(session).set_fact_enabled(
                    context.resource_scope,
                    namespace=namespace,
                    fact_id=fact_id,
                    expected_version=expected_version,
                    enabled=enabled,
                    now=datetime.now(UTC),
                )
                await self._audit_change(
                    session,
                    context,
                    resource_id=fact_id,
                    operation="fact_restore" if enabled else "fact_disable",
                )
                return result
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None

    @staticmethod
    def _fact_lineage_payload(
        context: PrivateWorkContext,
        *,
        namespace: str,
        fact_id: uuid.UUID,
    ) -> bytes:
        return _FACT_LINEAGE_HMAC_DOMAIN + json.dumps(
            {
                "fact_id": str(fact_id),
                "namespace": namespace,
                "owner_user_id": str(context.user_id),
                "project_id": str(context.project_id),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    async def hard_forget_fact(
        self,
        context: PrivateWorkContext,
        fact_id: uuid.UUID,
        *,
        namespace: str,
        expected_version: int,
    ) -> MemoryV2HardForgetResult:
        context = self._context(context)
        try:
            reference = self._source_hmac(
                self._fact_lineage_payload(
                    context,
                    namespace=namespace,
                    fact_id=fact_id,
                )
            )
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                    lock=True,
                )
                result = await self._repository_builder(session).hard_forget_fact(
                    context.resource_scope,
                    namespace=namespace,
                    fact_id=fact_id,
                    expected_version=expected_version,
                    lineage_identity_hmac=reference.hmac_hex,
                    lineage_hmac_key_version=reference.key_id,
                    now=datetime.now(UTC),
                )
                await self._audit_change(
                    session,
                    context,
                    resource_id=fact_id,
                    operation="fact_hard_forget",
                    affected_count=(result.erased_candidates + result.erased_revisions + result.erased_evidence + result.erased_source_items),
                )
                return result
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            raise self._map_error(context, error) from None
