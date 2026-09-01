"""Knowledge byte facts adapted to the single host project quota service."""

from dataclasses import dataclass
from uuid import UUID

from actweave_knowledge.contracts import KNOWLEDGE_CONFLICT, KNOWLEDGE_QUOTA_EXCEEDED, KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from actweave_knowledge.persistence.models import KnowledgeAttachmentRow, KnowledgeDocumentRow, KnowledgeExtractionRow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.quotas.models import QuotaConflict, QuotaExceeded, QuotaUnavailable, _issue_project_storage_quota_authority
from app.quotas.service import QuotaService
from deerflow.persistence.projects.model import ProjectRow


@dataclass(frozen=True, slots=True)
class KnowledgeObjectFact:
    row: KnowledgeDocumentRow | KnowledgeAttachmentRow | KnowledgeExtractionRow

    @property
    def project_id(self) -> UUID:
        return self.row.project_id

    @property
    def size_bytes(self) -> int:
        return self.row.manifest_size_bytes if isinstance(self.row, KnowledgeExtractionRow) else self.row.size_bytes

    @property
    def upload_state(self) -> str:
        return self.row.manifest_upload_state if isinstance(self.row, KnowledgeExtractionRow) else self.row.upload_state

    @property
    def quota_state(self) -> str:
        return self.row.manifest_quota_state if isinstance(self.row, KnowledgeExtractionRow) else self.row.quota_state

    def set_quota_state(self, state: str) -> None:
        if isinstance(self.row, KnowledgeExtractionRow):
            self.row.manifest_quota_state = state
        else:
            self.row.quota_state = state


def _conflict() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_CONFLICT, "Knowledge 存储配额状态冲突")


async def load_object_fact(session: AsyncSession, object_id: UUID, *, for_update: bool) -> KnowledgeObjectFact:
    """Resolve exactly one UUID; lock Project before any owning business row.

    The first read only discovers Project identity, never authorizes mutation.
    The locked second read rejects deletion, replacement, and UUID ambiguity.
    """
    models = (KnowledgeDocumentRow, KnowledgeAttachmentRow, KnowledgeExtractionRow)

    async def load(locked: bool) -> KnowledgeObjectFact:
        matches = []
        for model in models:
            query = select(model).where(model.id == object_id)
            if locked:
                query = query.with_for_update().execution_options(populate_existing=True)
            row = await session.scalar(query)
            if row is not None:
                matches.append(row)
        if len(matches) != 1:
            raise _conflict()
        row = matches[0]
        if isinstance(row, KnowledgeExtractionRow) and row.manifest_storage_key is None:
            raise _conflict()
        return KnowledgeObjectFact(row)

    fact = await load(False)
    if for_update:
        project_id = fact.project_id
        if await session.scalar(select(ProjectRow.id).where(ProjectRow.id == project_id).with_for_update(read=True, of=ProjectRow)) is None:
            raise _conflict()
        fact = await load(True)
        if fact.project_id != project_id:
            raise _conflict()
    return fact


class HostKnowledgeStorageQuotaPort:
    def __init__(self, quotas: QuotaService) -> None:
        if type(quotas) is not QuotaService:
            raise TypeError("Knowledge storage requires the host QuotaService")
        self._quotas = quotas

    async def reserve(self, session: AsyncSession, *, project_id: UUID, object_id: UUID, size_bytes: int) -> None:
        fact = await load_object_fact(session, object_id, for_update=True)
        if type(size_bytes) is not int or size_bytes < 0 or fact.project_id != project_id or fact.size_bytes != size_bytes or fact.quota_state == "released":
            raise _conflict()
        if fact.quota_state in {"reserved", "committed"}:
            return
        if fact.upload_state not in {"pending", "stored"}:
            raise _conflict()
        try:
            if size_bytes:
                mutation = await self._quotas.mutate_project_storage(session, _issue_project_storage_quota_authority(project_id, operation="reserve"), size_bytes, f"knowledge-object:{object_id}")
                if not mutation.created:
                    raise QuotaConflict("object reservation already exists")
            fact.set_quota_state("reserved")
            await session.flush()
        except QuotaExceeded:
            raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "项目存储配额不足") from None
        except QuotaConflict:
            raise _conflict() from None
        except QuotaUnavailable:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储配额暂时不可用") from None

    async def commit(self, session: AsyncSession, *, object_id: UUID) -> None:
        fact = await load_object_fact(session, object_id, for_update=True)
        if fact.upload_state != "stored" or fact.quota_state not in {"reserved", "committed"}:
            raise _conflict()
        if fact.quota_state == "committed":
            return
        try:
            if fact.size_bytes:
                await self._quotas.commit_project_storage(session, _issue_project_storage_quota_authority(fact.project_id, operation="commit"), fact.size_bytes, f"knowledge-object:{object_id}")
            fact.set_quota_state("committed")
            await session.flush()
        except QuotaConflict:
            raise _conflict() from None
        except QuotaUnavailable:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储配额暂时不可用") from None

    async def release(self, session: AsyncSession, *, object_id: UUID) -> None:
        fact = await load_object_fact(session, object_id, for_update=True)
        if fact.upload_state != "deleted":
            raise _conflict()
        if fact.quota_state == "released":
            return
        try:
            if fact.size_bytes and fact.quota_state in {"reserved", "committed"}:
                await self._quotas.mutate_project_storage(
                    session, _issue_project_storage_quota_authority(fact.project_id, operation="release"), fact.size_bytes, f"knowledge-object:{object_id}", storage_axis="used" if fact.quota_state == "committed" else "reserved"
                )
            fact.set_quota_state("released")
            await session.flush()
        except QuotaConflict:
            raise _conflict() from None
        except QuotaUnavailable:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储配额暂时不可用") from None
