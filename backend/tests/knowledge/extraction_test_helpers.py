"""Shared Extraction integration helpers over fixture-prepared Schema V1."""

import asyncio
import hashlib
import hmac
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from actweave_knowledge.contracts import KNOWLEDGE_NOT_FOUND, KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from actweave_knowledge.persistence.models import KnowledgeAttachmentRow, KnowledgeDocumentRow, KnowledgeExtractionRow, KnowledgeSegmentAttachmentRow, KnowledgeSegmentRow, KnowledgeTaskRow
from actweave_knowledge.persistence.tasks import claim_next_task
from actweave_knowledge.tasks.worker import KnowledgeTaskClaim
from registry_helpers import seed_embedding_model, seed_provider
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.quotas.models import QuotaSourceRef
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig


@asynccontextmanager
async def installed_knowledge_sessions(url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Open sessions on the isolated, already installed database fixture."""
    engine = create_async_engine(url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def seed_scope(sessions: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create independent identity, registry binding, Base and Document facts."""

    project_id, base_id, document_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user_id = str(uuid.uuid4())
    model_id = await seed_embedding_model(sessions, await seed_provider(sessions))
    async with sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO users
            (id,email,username,system_role,created_at,needs_setup,token_version)
            VALUES (:id,:email,:username,'user',now(),false,1)"""),
            {"id": user_id, "email": f"{user_id}@example.invalid", "username": f"u{uuid.uuid4().hex[:24]}"},
        )
        await session.execute(
            text("""INSERT INTO projects (id,slug,display_name,created_by_user_id)
            VALUES (:id,:slug,'Extraction schema',:user_id)"""),
            {"id": project_id, "slug": f"ext-{project_id.hex}", "user_id": user_id},
        )
        await session.execute(
            text("""INSERT INTO knowledge_bases (id,project_id,name,embedding_model_id)
            VALUES (:id,:project_id,'Schema Base',:model_id)"""),
            {"id": base_id, "project_id": project_id, "model_id": model_id},
        )
        await session.execute(
            text("""INSERT INTO knowledge_documents
            (id,project_id,knowledge_base_id,name,original_name,storage_key,size_bytes,upload_state,quota_state)
            VALUES (:id,:project_id,:base_id,'source.pdf','source.pdf',:key,64,'stored','committed')"""),
            {"id": document_id, "project_id": project_id, "base_id": base_id, "key": f"sources/{document_id}"},
        )
    return project_id, base_id, document_id


# Storage bytes are deterministic test doubles; quota and claims are real.


def test_quota_hash(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(key_id="extraction-test", hmac_hex=hmac.new(b"extraction-fixture-key", payload, hashlib.sha256).hexdigest())


test_quota_hash.__test__ = False


@dataclass
class ObjectIOBarrier:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def enter(self) -> None:
        self.entered.set()
        await self.released.wait()


class ExtractionObjectStore:
    """Byte store with exact-key failure injection and awaitable I/O barriers."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self._failures: set[str] = set()
        self._barriers: dict[str, ObjectIOBarrier] = {}

    def fail_next(self, operation: str) -> None:
        self._failures.add(operation)

    def pause(self, operation: str) -> ObjectIOBarrier:
        barrier = ObjectIOBarrier()
        self._barriers[operation] = barrier
        return barrier

    async def _before(self, operation: str, key: str) -> None:
        self.calls.append((operation, key))
        barrier = self._barriers.pop(operation, None)
        if barrier is not None:
            await barrier.enter()
        if operation in self._failures:
            self._failures.remove(operation)
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储暂时不可用")

    async def upload_from(self, key: str, source_path: Path, *, media_type: str | None = None) -> None:
        await self._before("put", key)
        self.objects[key] = source_path.read_bytes()

    async def download_to(self, key: str, target_path: Path, *, max_bytes: int | None = None) -> None:
        await self._before("get", key)
        if key not in self.objects:
            from actweave_knowledge.storage.minio_store import ObjectMissingError

            raise ObjectMissingError()
        if max_bytes is not None and len(self.objects[key]) > max_bytes:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储暂时不可用")
        target_path.write_bytes(self.objects[key])

    async def stat_object(self, key: str):
        from actweave_knowledge.storage.minio_store import StoredObjectInfo

        await self._before("get", key)
        if key not in self.objects:
            from actweave_knowledge.storage.minio_store import ObjectMissingError

            raise ObjectMissingError()
        payload = self.objects[key]
        return StoredObjectInfo(len(payload), hashlib.sha256(payload).hexdigest())

    async def delete_many(self, keys: list[str]) -> None:
        await self.require_unversioned_bucket()
        for key in keys:
            await self._delete_after_bucket_check(key)

    async def delete_project_objects(self, project_id: uuid.UUID) -> None:
        await self.delete_many([key for key in self.objects if key.startswith(f"projects/{project_id}/knowledge/")])

    async def delete(self, key: str) -> None:
        await self.require_unversioned_bucket()
        await self._delete_after_bucket_check(key)

    async def _delete_after_bucket_check(self, key: str) -> None:
        await self._before("delete", key)
        self.objects.pop(key, None)

    async def require_absent(self, key: str) -> None:
        await self._before("get", key)
        if key in self.objects:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储删除结果无法确认")

    async def require_unversioned_bucket(self) -> None:
        await self._before("bucket", "")


@dataclass
class ToggleKnowledgeAuthority:
    """Revocable package read authority backed by the live Project row."""

    project_id: uuid.UUID
    actor_user_id: uuid.UUID
    revoked: bool = False

    async def revalidate(self, session: AsyncSession) -> None:
        active = await session.scalar(
            text("SELECT status = 'active' FROM projects WHERE id = :id"),
            {"id": self.project_id},
        )
        if self.revoked or not active:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")


@dataclass
class ExtractionHarness:
    session_factory: async_sessionmaker[AsyncSession]
    claim: KnowledgeTaskClaim
    project_id: uuid.UUID
    base_id: uuid.UUID
    document_id: uuid.UUID
    object_store: ExtractionObjectStore
    quota: Any
    quota_service: QuotaService
    _store: Any = field(default=None, init=False, repr=False)

    @property
    def store(self):
        if self._store is None:
            from actweave_knowledge.storage.extractions import ExtractionStore

            from app.knowledge.composition import is_knowledge_project_active

            self._store = ExtractionStore(session_factory=self.session_factory, object_store=self.object_store, quota=self.quota, project_active_check=is_knowledge_project_active, cache_enabled=True)
        return self._store

    async def read_rows(self) -> dict[str, list]:
        async with self.session_factory() as session:
            return {
                key: list((await session.scalars(select(model).where(model.project_id == self.project_id))).all())
                for key, model in (("documents", KnowledgeDocumentRow), ("extractions", KnowledgeExtractionRow), ("attachments", KnowledgeAttachmentRow), ("bindings", KnowledgeSegmentAttachmentRow), ("tasks", KnowledgeTaskRow))
            }

    async def published_result(self):
        """Persist a complete read/delete fixture, without simulating indexing."""
        from actweave_knowledge.persistence.tasks import settle_task_success
        from parsing_test_helpers import make_parse_profile

        source = (await self.read_rows())["documents"][0].source_sha256
        profile = make_parse_profile(".pdf")
        reservation = await self.store.begin(self.claim, source_sha256=source, profile=profile)
        stored = await self.store.complete(reservation, make_extraction_result(profile, source_sha256=source))
        async with self.session_factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, self.document_id, with_for_update=True)
            document.published_extraction_id = stored.extraction_id
            document.published_version = document.version
            document.status = "ready"
            assert await settle_task_success(session, self.claim.id, self.claim.claim_token)
        return stored

    async def bind_test_attachment(self, stored, asset) -> tuple[uuid.UUID, uuid.UUID, str]:
        """Bind a ready stored image occurrence to the published Segment."""

        if stored.document_id != self.document_id:
            raise AssertionError("stored extraction belongs to another document")
        content = stored.result.documents[0].page_content
        if f"knowledge-attachment:{asset.attachment.ref}" not in content:
            raise AssertionError("segment content must contain the attachment reference")
        async with self.session_factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, self.document_id)
            if document is None or document.published_extraction_id != stored.extraction_id or document.published_version is None:
                raise AssertionError("stored extraction must be the published extraction")
            attachment = await session.scalar(
                select(KnowledgeAttachmentRow).where(
                    KnowledgeAttachmentRow.project_id == self.project_id,
                    KnowledgeAttachmentRow.knowledge_base_id == self.base_id,
                    KnowledgeAttachmentRow.knowledge_document_id == self.document_id,
                    KnowledgeAttachmentRow.extraction_id == stored.extraction_id,
                    KnowledgeAttachmentRow.sha256 == asset.attachment.ref,
                    KnowledgeAttachmentRow.state == "ready",
                    KnowledgeAttachmentRow.upload_state == "stored",
                )
            )
            if attachment is None:
                raise AssertionError("ready stored attachment is missing")
            segment_id = uuid.uuid4()
            session.add(
                KnowledgeSegmentRow(
                    id=segment_id,
                    project_id=self.project_id,
                    knowledge_base_id=self.base_id,
                    knowledge_document_id=self.document_id,
                    extraction_id=stored.extraction_id,
                    document_version=document.published_version,
                    position=1,
                    content=content,
                )
            )
            await session.flush()
            session.add(
                KnowledgeSegmentAttachmentRow(
                    project_id=self.project_id,
                    knowledge_base_id=self.base_id,
                    knowledge_document_id=self.document_id,
                    extraction_id=stored.extraction_id,
                    segment_id=segment_id,
                    attachment_id=attachment.id,
                    position=1,
                )
            )
        return segment_id, attachment.id, hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def seed_attachment_read(self, work_dir: Path):
        """Create one complete Extraction, publish it, and bind its image."""

        from actweave_knowledge.persistence.tasks import settle_task_success
        from parsing_test_helpers import make_parse_profile

        asset = write_test_asset(work_dir)
        source_sha256 = (await self.read_rows())["documents"][0].source_sha256
        profile = make_parse_profile(".pdf")
        reservation = await self.store.begin(self.claim, source_sha256=source_sha256, profile=profile)
        await self.store.persist_attachment(reservation, asset, work_dir=work_dir)
        stored = await self.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source_sha256, attachments=(asset,)),
        )
        async with self.session_factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, self.document_id)
            document.published_extraction_id = stored.extraction_id
            document.published_version = document.version
            document.status = "ready"
            assert await settle_task_success(session, self.claim.id, self.claim.claim_token)
        segment_id, attachment_id, digest = await self.bind_test_attachment(stored, asset)
        return segment_id, attachment_id, digest, ToggleKnowledgeAuthority(self.project_id, uuid.uuid4())

    async def claim_cleanup(self, extraction_id: uuid.UUID) -> KnowledgeTaskClaim:
        """Admit and claim the durable cleanup task through production paths."""

        await self.store.enqueue_cleanup(extraction_id, project_id=self.project_id)
        async with self.session_factory() as session, session.begin():
            task = await claim_next_task(session, lease_seconds=600)
            assert task is not None and task.claim_token is not None
            assert (task.kind, task.resource_id, task.project_id, task.storage_key) == (
                "delete_extraction",
                extraction_id,
                self.project_id,
                None,
            )
            return KnowledgeTaskClaim(
                id=task.id,
                project_id=task.project_id,
                resource_id=task.resource_id,
                kind=task.kind,
                target_version=task.target_version,
                claim_token=task.claim_token,
                attempt_count=task.attempt_count,
                max_attempts=task.max_attempts,
                storage_key=task.storage_key,
                reparse_settings=task.reparse_settings,
            )

    async def register_test_attachment(self, size_bytes: int, upload_state: str = "pending") -> uuid.UUID:
        attachment_id = uuid.uuid4()
        async with self.session_factory() as session, session.begin():
            extraction_id = await session.scalar(select(KnowledgeExtractionRow.id).where(KnowledgeExtractionRow.created_task_id == self.claim.id, KnowledgeExtractionRow.created_attempt == self.claim.attempt_count))
            if extraction_id is None:
                extraction_id = uuid.uuid4()
                session.add(
                    KnowledgeExtractionRow(
                        id=extraction_id,
                        project_id=self.project_id,
                        knowledge_base_id=self.base_id,
                        knowledge_document_id=self.document_id,
                        source_sha256=hashlib.sha256(b"original").hexdigest(),
                        parser_fingerprint="b" * 64,
                        normalization_version="normalize_v1",
                        state="staging",
                        created_task_id=self.claim.id,
                        created_attempt=self.claim.attempt_count,
                        created_claim_token=self.claim.claim_token,
                        target_document_version=1,
                    )
                )
            await session.flush()
            session.add(
                KnowledgeAttachmentRow(
                    id=attachment_id,
                    extraction_id=extraction_id,
                    project_id=self.project_id,
                    knowledge_base_id=self.base_id,
                    knowledge_document_id=self.document_id,
                    sha256=hashlib.sha256(str(attachment_id).encode()).hexdigest(),
                    media_type="image/png",
                    size_bytes=size_bytes,
                    width=1,
                    height=1,
                    storage_key=f"attachments/{attachment_id}",
                    state="staging",
                    upload_state=upload_state,
                )
            )
        return attachment_id


@asynccontextmanager
async def extraction_harness(postgres_database_url: str, *, quota_bytes: int = 524288000):
    from app.knowledge.quota_port import HostKnowledgeStorageQuotaPort

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        project_id, base_id, document_id = await seed_scope(sessions)
        quota_service = QuotaService(session_factory=sessions, config=QuotaConfig(default_storage_bytes_limit=quota_bytes), source_ref_hasher=test_quota_hash)
        quota = HostKnowledgeStorageQuotaPort(quota_service)
        object_store = ExtractionObjectStore()
        async with sessions() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, document_id)
            document.status = "queued"
            document.size_bytes = 8
            document.source_sha256 = hashlib.sha256(b"original").hexdigest()
            document.quota_state = "unreserved"
            await session.flush()
            await quota.reserve(session, project_id=project_id, object_id=document_id, size_bytes=8)
            await quota.commit(session, object_id=document_id)
            object_store.objects[document.storage_key] = b"original"
            session.add(KnowledgeTaskRow(id=uuid.uuid4(), project_id=project_id, resource_id=document_id, kind="ingest_document", target_version=1))
            await session.flush()
            task = await claim_next_task(session, lease_seconds=600)
            assert task is not None and task.claim_token is not None
            claim = KnowledgeTaskClaim(
                id=task.id,
                project_id=task.project_id,
                resource_id=task.resource_id,
                kind=task.kind,
                target_version=task.target_version,
                claim_token=task.claim_token,
                attempt_count=task.attempt_count,
                max_attempts=task.max_attempts,
                storage_key=task.storage_key,
                reparse_settings=task.reparse_settings,
            )
        yield ExtractionHarness(sessions, claim, project_id, base_id, document_id, object_store, quota, quota_service)


def make_test_quota_service(session_factory, *, quota_bytes: int = 524288000) -> QuotaService:
    """Real host quota for existing constructor fixtures; no object I/O."""
    return QuotaService(session_factory=session_factory, config=QuotaConfig(default_storage_bytes_limit=quota_bytes), source_ref_hasher=test_quota_hash)


def make_test_quota_port(session_factory):
    from app.knowledge.quota_port import HostKnowledgeStorageQuotaPort

    return HostKnowledgeStorageQuotaPort(make_test_quota_service(session_factory))


def write_test_asset(work_dir: Path):
    from actweave_knowledge.extraction import Attachment, LocalAttachment
    from PIL import Image

    path = work_dir / "asset.png"
    Image.new("RGB", (1, 1), "red").save(path, format="PNG")
    payload = path.read_bytes()
    return LocalAttachment(attachment=Attachment(ref=hashlib.sha256(payload).hexdigest(), media_type="image/png", size_bytes=len(payload), width=1, height=1), relative_path=path.name)


def make_extraction_result(profile, *, source_sha256, attachments=()):
    """Complete deterministic pages, warning and repeated image provenance."""
    from actweave_knowledge.extraction import AttachmentOccurrence, ExtractionResult, ParseWarning, SourceSpan, canonical_parse_fingerprint
    from parsing_test_helpers import make_document

    warning = ParseWarning(code="IMAGE_EXTERNAL_SKIPPED", message="未抓取外链图片", source_position={"page": 2})
    documents = []
    for page in (1, 2):
        content = f"第{page}页正文"
        occurrences = []
        for index, asset in enumerate(attachments, 1):
            image = f"![图{index}](knowledge-attachment:{asset.attachment.ref})"
            start = len(content) + 2
            content += "\n\n" + image
            occurrences.append(AttachmentOccurrence(ref=asset.attachment.ref, alt_text=f"图{index}", source=SourceSpan(block_id=f"page:{page}:image:{index}", start=start, end=len(content), location={"page": page, "image_index": index})))
        documents.append(make_document(content, location={"page": page}).model_copy(update={"attachments": tuple(occurrences), "warnings": (warning,) if page == 2 else ()}))
    return ExtractionResult(documents=tuple(documents), attachments=tuple(asset.attachment for asset in attachments), warnings=(warning,), source_sha256=source_sha256, parse_fingerprint=canonical_parse_fingerprint(profile))


def make_test_file_capabilities(settings=None):
    """Static ready process snapshot for package service tests."""
    from actweave_knowledge.contracts import KnowledgeSettings
    from actweave_knowledge.extraction.registry import default_registry
    from actweave_knowledge.ingestion.profiles import build_file_capabilities

    return build_file_capabilities(settings or KnowledgeSettings(), default_registry())


def make_test_file_capability_provider(settings=None):
    """Return one immutable process snapshot, matching production composition."""
    capabilities = make_test_file_capabilities(settings)
    return lambda: capabilities
