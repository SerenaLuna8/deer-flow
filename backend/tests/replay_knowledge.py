"""Test-only Knowledge harness for the real-backend browser gate.

Owns everything the replay Gateway needs to run the Knowledge module for
real — deterministic mock SiliconFlow provider (``/v1/embeddings`` +
``/v1/rerank``) on an ephemeral loopback port, one disposable MinIO bucket,
the pgvector extension, the seeded model registry (one Provider plus one
embedding and one rerank model) pointing at the mock, and the
``/api/test-only/replay-knowledge`` control router used by the Playwright
specs to inject provider faults and verify object-store contents.

Determinism contract shared with ``frontend/tests/e2e-real-backend``:

- Embeddings are axis vectors: texts containing :data:`DOC_RERANK_MARKER`
  land on axis 0, every other text (including queries that avoid the marker)
  lands on axis 1. Cosine recall therefore ranks marker segments *last*.
- The reranker scores marker documents 0.95 and everything else below 0.6,
  so the final citation order provably comes from the rerank stage, never
  from cosine order.

No ``from __future__ import annotations`` here: the FastAPI handlers below
are defined inside builder functions, and postponed annotations would leave
their parameter types as unresolvable strings (FastAPI would then read the
request body parameters as query parameters).
"""

import asyncio
import hashlib
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_MINIO_ENDPOINT_ENV = "ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT"
_MINIO_ACCESS_KEY_ENV = "ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY"
_MINIO_SECRET_KEY_ENV = "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY"
_STORAGE_CONTROL_ROOT_ENV = "ACT_WEAVE_REPLAY_KNOWLEDGE_CONTROL_ROOT"

_REPLAY_KNOWLEDGE_NAMESPACE = uuid.UUID("9a1de0f4-63b7-5c58-9f2e-0d84a3c14b72")

REPLAY_KNOWLEDGE_MODEL_DISPLAY_NAME = "Replay Knowledge Model"
REPLAY_KNOWLEDGE_MODEL_API_KEY = "sk-replay-knowledge-mock"
REPLAY_EMBEDDING_DIMENSION = 64
REPLAY_SUMMARY_MODEL_DISPLAY_NAME = "Replay Knowledge Summary"
REPLAY_SUMMARY_OUTPUT_MARKER = "摘要索引回放"

# Documents containing this exact substring get the far-from-query embedding
# (cosine ranks them last) but the top rerank score (final order ranks them
# first). Specs must keep the marker out of their queries.
DOC_RERANK_MARKER = "深海列车"
RERANK_MARKER_SCORE = 0.95

_STORAGE_COUNTERS = frozenset(
    {
        "source_reads",
        "manifest_reads",
        "attachment_reads",
        "source_put_failures",
        "manifest_put_failures",
        "attachment_put_failures",
        "delete_failures",
    }
)


class ReplayKnowledgeStorageControl:
    """Cross-process replay counters containing no keys, secrets, or bytes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "ReplayKnowledgeStorageControl | None":
        raw = os.environ.get(_STORAGE_CONTROL_ROOT_ENV, "").strip()
        return cls(Path(raw)) if raw else None

    def _mutate(self, name: str, update: Callable[[int], int]) -> int:
        if name not in _STORAGE_COUNTERS:
            raise ValueError("unsupported replay storage counter")
        import fcntl

        path = self.root / f"{name}.count"
        with self._thread_lock, path.open("a+", encoding="ascii") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                current = int(raw) if raw else 0
                next_value = update(current)
                if next_value < 0:
                    raise ValueError("replay storage counter cannot be negative")
                handle.seek(0)
                handle.truncate()
                handle.write(str(next_value))
                handle.flush()
                return next_value
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self, name: str) -> int:
        return self._mutate(name, lambda current: current)

    def set(self, name: str, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("replay storage counter must be a non-negative integer")
        self._mutate(name, lambda _current: value)

    def increment(self, name: str) -> None:
        self._mutate(name, lambda current: current + 1)

    def consume(self, name: str) -> bool:
        consumed = False

        def decrement(current: int) -> int:
            nonlocal consumed
            consumed = current > 0
            return current - 1 if consumed else current

        self._mutate(name, decrement)
        return consumed

    def storage_snapshot(self) -> dict[str, int]:
        return {
            name: self.read(name)
            for name in (
                "source_reads",
                "manifest_reads",
                "attachment_reads",
            )
        }

    def replace_faults(
        self,
        *,
        source_put_failures: int,
        manifest_put_failures: int,
        attachment_put_failures: int,
        delete_failures: int,
    ) -> None:
        values = {
            "source_put_failures": source_put_failures,
            "manifest_put_failures": manifest_put_failures,
            "attachment_put_failures": attachment_put_failures,
            "delete_failures": delete_failures,
        }
        for name, value in values.items():
            self.set(name, value)


def _storage_object_kind(key: str) -> str:
    if key.endswith("/manifest.json"):
        return "manifest"
    if "/assets/" in key:
        return "attachment"
    return "source"


def install_replay_knowledge_storage_controls() -> None:
    """Install replay-only MinIO counters and faults in this test process."""

    control = ReplayKnowledgeStorageControl.from_environment()
    if control is None:
        return

    from functools import wraps

    from actweave_knowledge.contracts import (
        KNOWLEDGE_STORAGE_UNAVAILABLE,
        KnowledgeError,
    )
    from actweave_knowledge.storage import MinioObjectStore

    if getattr(MinioObjectStore, "_replay_storage_controls_installed", False):
        return

    original_upload = MinioObjectStore.upload_from
    original_download = MinioObjectStore.download_to
    original_delete = MinioObjectStore._delete_after_bucket_check

    @wraps(original_upload)
    async def controlled_upload(self, key, source_path, *, media_type=None):
        kind = _storage_object_kind(key)
        if control.consume(f"{kind}_put_failures"):
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "对象存储写入失败，请稍后重试",
            )
        return await original_upload(
            self,
            key,
            source_path,
            media_type=media_type,
        )

    @wraps(original_download)
    async def counted_download(self, key, target_path, *, max_bytes=None):
        control.increment(f"{_storage_object_kind(key)}_reads")
        return await original_download(
            self,
            key,
            target_path,
            max_bytes=max_bytes,
        )

    @wraps(original_delete)
    async def controlled_delete(self, key):
        if control.consume("delete_failures"):
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "对象存储删除失败，请稍后重试",
            )
        return await original_delete(self, key)

    MinioObjectStore.upload_from = controlled_upload
    MinioObjectStore.download_to = counted_download
    MinioObjectStore._delete_after_bucket_check = controlled_delete
    MinioObjectStore._replay_storage_controls_installed = True


def knowledge_minio_environment_ready() -> bool:
    """True when all three MinIO variables are present and non-empty."""

    return all(os.environ.get(name, "").strip() for name in (_MINIO_ENDPOINT_ENV, _MINIO_ACCESS_KEY_ENV, _MINIO_SECRET_KEY_ENV))


@dataclass(frozen=True, slots=True)
class ReplayMinioSettings:
    endpoint: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


def replay_minio_settings_from_environment() -> ReplayMinioSettings:
    if not knowledge_minio_environment_ready():
        raise RuntimeError("replay knowledge requires the three ACT_WEAVE_KNOWLEDGE_MINIO_* variables")
    return ReplayMinioSettings(
        endpoint=os.environ[_MINIO_ENDPOINT_ENV].strip(),
        access_key=os.environ[_MINIO_ACCESS_KEY_ENV].strip(),
        secret_key=os.environ[_MINIO_SECRET_KEY_ENV].strip(),
    )


def _minio_client(settings: ReplayMinioSettings):
    from minio import Minio

    return Minio(
        settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        secure=False,
    )


def create_replay_knowledge_bucket(settings: ReplayMinioSettings) -> str:
    """Create one random disposable bucket; the caller owns its removal."""

    bucket = f"deerflow-test-replay-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    _minio_client(settings).make_bucket(bucket)
    return bucket


def drop_replay_knowledge_bucket(settings: ReplayMinioSettings, bucket: str) -> None:
    """Remove every object in the disposable bucket, then the bucket itself."""

    client = _minio_client(settings)
    for item in client.list_objects(bucket, recursive=True):
        client.remove_object(bucket, item.object_name)
    client.remove_bucket(bucket)


def list_replay_knowledge_objects(settings: ReplayMinioSettings, bucket: str) -> list[str]:
    return sorted(item.object_name for item in _minio_client(settings).list_objects(bucket, recursive=True))


def prepare_pgvector_extension(database_url: str) -> None:
    """Install ``public.vector`` in the disposable replay database."""

    import psycopg
    from sqlalchemy.engine import make_url

    sync_url = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")


async def seed_replay_knowledge_settings(database_url: str, *, settings: ReplayMinioSettings, bucket: str, summary_model_name: str | None = None) -> None:
    """Seed database-owned configuration only in an isolated test database."""
    from _replay_fixture import _validated_replay_database_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.knowledge_settings.service import knowledge_minio_secret_recipient
    from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
    from deerflow.secrets import SecretEnvelope, SecretKey

    engine = create_async_engine(_validated_replay_database_url(database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        envelope = SecretEnvelope.protect(settings.secret_key.encode("utf-8"), recipient=knowledge_minio_secret_recipient(settings.endpoint), key=SecretKey.from_environment())
        async with factory() as session, session.begin():
            row = await session.get(KnowledgeSystemSettingsRow, 1, with_for_update=True)
            if row is None:
                row = KnowledgeSystemSettingsRow(id=1)
                session.add(row)
            row.enabled = True
            row.worker_concurrency = 2
            row.task_timeout_seconds = 120
            row.upload_max_bytes = 10485760
            row.minio_endpoint = settings.endpoint
            row.minio_bucket = bucket
            row.minio_access_key = settings.access_key
            row.minio_secure = False
            row.minio_secret_nonce = envelope.nonce
            row.minio_secret_ciphertext = envelope.ciphertext
            row.summary_model_name = summary_model_name
    finally:
        await engine.dispose()


async def seed_replay_summary_model(database_url: str) -> uuid.UUID:
    """Bind a live System Model to the loopback provider already seeded above."""
    from types import SimpleNamespace

    from _replay_fixture import _REPLAY_ADMIN_ID, _validated_replay_database_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.audit.models import resolve_system_audit_context
    from app.system_settings.models import CreateSystemModel
    from app.system_settings.service import SystemModelCatalogService

    engine = create_async_engine(_validated_replay_database_url(database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        context = resolve_system_audit_context(SimpleNamespace(id=_REPLAY_ADMIN_ID, system_role="system_admin"), request_id="replay-summary-model")
        catalog = SystemModelCatalogService(factory)
        for model in (await catalog.list_models(context)).items:
            if model.display_name == REPLAY_SUMMARY_MODEL_DISPLAY_NAME:
                return model.id
        model = await catalog.create_model(
            context,
            CreateSystemModel(
                display_name=REPLAY_SUMMARY_MODEL_DISPLAY_NAME,
                status="active",
                provider_id=uuid.uuid5(_REPLAY_KNOWLEDGE_NAMESPACE, "replay-model-provider"),
                provider_adapter="knowledge_replay",
                provider_model="replay/summary",
                max_input_tokens=64000,
                settings={},
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=False,
            ),
        )
        return model.id
    finally:
        await engine.dispose()


async def seed_replay_model_registry(database_url: str, *, base_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert the mock-backed Provider plus its embedding and rerank models.

    Direct ORM inserts into the empty disposable database, with the API Key
    encrypted exactly like the host registry stores it (the replay process
    always exports ``ACT_WEAVE_SECRET_KEY``). Returns
    ``(embedding_model_id, rerank_model_id)``.
    """

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.model_registry.secrets import protect_provider_api_key
    from deerflow.persistence.model_registry import (
        ModelProviderModelRow,
        ModelProviderRow,
    )
    from deerflow.secrets import SecretKey

    provider_id = uuid.uuid5(_REPLAY_KNOWLEDGE_NAMESPACE, "replay-model-provider")
    embedding_model_id = uuid.uuid5(_REPLAY_KNOWLEDGE_NAMESPACE, "replay-embedding-model")
    rerank_model_id = uuid.uuid5(_REPLAY_KNOWLEDGE_NAMESPACE, "replay-rerank-model")
    envelope = protect_provider_api_key(
        provider_id=provider_id,
        base_url=base_url,
        api_key=REPLAY_KNOWLEDGE_MODEL_API_KEY,
        key=SecretKey.from_environment(),
    )
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            session.add(
                ModelProviderRow(
                    id=provider_id,
                    name=REPLAY_KNOWLEDGE_MODEL_DISPLAY_NAME,
                    base_url=base_url,
                    request_timeout_seconds=10,
                    api_key_nonce=envelope.nonce,
                    api_key_ciphertext=envelope.ciphertext,
                )
            )
            # No relationship() links the registry mappers, so the unit of
            # work will not order these inserts by the FK; flush the Provider
            # before its models.
            await session.flush()
            session.add(
                ModelProviderModelRow(
                    id=embedding_model_id,
                    provider_id=provider_id,
                    model_type="embedding",
                    model_name="replay/embedding",
                    embedding_dimension=REPLAY_EMBEDDING_DIMENSION,
                    max_batch=8,
                    status="active",
                )
            )
            session.add(
                ModelProviderModelRow(
                    id=rerank_model_id,
                    provider_id=provider_id,
                    model_type="rerank",
                    model_name="replay/reranker",
                    embedding_dimension=None,
                    max_batch=8,
                    status="active",
                )
            )
    finally:
        await engine.dispose()
    return embedding_model_id, rerank_model_id


# ---------------------------------------------------------------------------
# Deterministic mock SiliconFlow provider
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeReplayState:
    """Provider counters and fault injection shared with the control router."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    embedding_calls: int = 0
    rerank_calls: int = 0
    chat_calls: int = 0
    embedding_failures_remaining: int = 0
    rerank_failures_remaining: int = 0
    chat_failures_remaining: int = 0
    embedding_blocked: bool = False
    embedding_waiters: int = 0
    condition: threading.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)

    def snapshot(self) -> dict[str, int | bool]:
        with self.lock:
            return {
                "embedding_calls": self.embedding_calls,
                "rerank_calls": self.rerank_calls,
                "chat_calls": self.chat_calls,
                "embedding_failures_remaining": self.embedding_failures_remaining,
                "rerank_failures_remaining": self.rerank_failures_remaining,
                "chat_failures_remaining": self.chat_failures_remaining,
                "embedding_blocked": self.embedding_blocked,
                "embedding_waiters": self.embedding_waiters,
            }

    def replace_faults(
        self,
        *,
        embedding_failures: int,
        rerank_failures: int,
        chat_failures: int,
        embedding_blocked: bool,
    ) -> None:
        with self.condition:
            self.embedding_failures_remaining = embedding_failures
            self.rerank_failures_remaining = rerank_failures
            self.chat_failures_remaining = chat_failures
            self.embedding_blocked = embedding_blocked
            if not embedding_blocked:
                self.condition.notify_all()

    def wait_for_embedding_release(self) -> None:
        with self.condition:
            if not self.embedding_blocked:
                return
            self.embedding_waiters += 1
            try:
                while self.embedding_blocked:
                    self.condition.wait()
            finally:
                self.embedding_waiters -= 1


def replay_embedding(text: str, dimension: int) -> list[float]:
    """Axis-vector embedding: marker texts on axis 0, everything else axis 1.

    A small axis-1 component on marker texts keeps their cosine similarity to
    queries strictly positive, so they always survive candidate recall while
    still ranking last by vector score.
    """

    vector = [0.0] * dimension
    if DOC_RERANK_MARKER in text:
        vector[0] = 1.0
        vector[1] = 0.05
    else:
        vector[1] = 1.0
    return vector


def replay_rerank_results(documents: list[str], top_n: int) -> list[dict[str, float | int]]:
    """Marker documents score 0.95; the rest decay below 0.6 by input order."""

    scored = sorted(
        (
            {
                "index": index,
                "relevance_score": (RERANK_MARKER_SCORE if DOC_RERANK_MARKER in document else max(0.1, 0.55 - 0.02 * index)),
            }
            for index, document in enumerate(documents)
        ),
        key=lambda item: (-item["relevance_score"], item["index"]),
    )
    return scored[:top_n]


def _build_provider_app(state: KnowledgeReplayState):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        with state.lock:
            state.chat_calls += 1
            if state.chat_failures_remaining > 0:
                state.chat_failures_remaining -= 1
                return JSONResponse({"error": "replay summary fault"}, status_code=500)
        prompt = body["messages"][-1]["content"]
        source = prompt.split("源段落：\n", 1)[-1]
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        content = f"{REPLAY_SUMMARY_OUTPUT_MARKER} {digest}"
        return {
            "id": f"chatcmpl-replay-{digest}",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        with state.lock:
            state.embedding_calls += 1
            if state.embedding_failures_remaining > 0:
                state.embedding_failures_remaining -= 1
                return JSONResponse({"error": "replay embedding fault"}, status_code=500)
        await asyncio.to_thread(state.wait_for_embedding_release)
        texts = body["input"]
        dimension = int(body["dimensions"])
        return {
            "object": "list",
            "model": body.get("model"),
            "data": [{"object": "embedding", "index": index, "embedding": replay_embedding(text, dimension)} for index, text in enumerate(texts)],
        }

    @app.post("/v1/rerank")
    async def rerank(request: Request):
        body = await request.json()
        with state.lock:
            state.rerank_calls += 1
            if state.rerank_failures_remaining > 0:
                state.rerank_failures_remaining -= 1
                return JSONResponse({"error": "replay rerank fault"}, status_code=500)
        return {"results": replay_rerank_results(list(body["documents"]), int(body["top_n"]))}

    return app


class ReplayKnowledgeProviderServer:
    """Mock provider on an ephemeral loopback port, in a daemon thread.

    Running outside the Gateway app keeps provider calls away from the
    Gateway's auth/CSRF middleware: the Worker subprocess talks to it exactly
    like it would talk to SiliconFlow.
    """

    def __init__(self, state: KnowledgeReplayState) -> None:
        self._state = state
        self._server = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("replay knowledge provider is not running")
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self, *, timeout_seconds: float = 15) -> int:
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(
            _build_provider_app(self._state),
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [sock]},
            name="replay-knowledge-provider",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + timeout_seconds
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("replay knowledge provider did not start")
            time.sleep(0.02)
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None


# ---------------------------------------------------------------------------
# Gateway control router used by the Playwright specs
# ---------------------------------------------------------------------------


def build_replay_knowledge_router(
    state: KnowledgeReplayState,
    *,
    list_objects: Callable[[], list[str]],
    storage_control: ReplayKnowledgeStorageControl,
):
    """Build the ``/api/test-only/replay-knowledge`` control API."""

    from actweave_knowledge.persistence.models import (
        KnowledgeAttachmentRow,
        KnowledgeDocumentRow,
        KnowledgeExtractionRow,
        KnowledgeTaskRow,
    )
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, ConfigDict, Field
    from sqlalchemy import func, select

    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.projects.model import ProjectRow
    from deerflow.persistence.quotas.model import ProjectUsageCounterRow

    class ProviderFaults(BaseModel):
        model_config = ConfigDict(extra="forbid")

        embedding_failures: int = Field(default=0, ge=0, le=1000)
        rerank_failures: int = Field(default=0, ge=0, le=1000)
        chat_failures: int = Field(default=0, ge=0, le=1000)
        embedding_blocked: bool = False

    class StorageFaults(BaseModel):
        model_config = ConfigDict(extra="forbid")

        source_put_failures: int = Field(default=0, ge=0, le=1000)
        manifest_put_failures: int = Field(default=0, ge=0, le=1000)
        attachment_put_failures: int = Field(default=0, ge=0, le=1000)
        delete_failures: int = Field(default=0, ge=0, le=1000)

    class ProjectAuthorityFault(BaseModel):
        model_config = ConfigDict(extra="forbid")

        revoked: bool

    router = APIRouter(
        prefix="/api/test-only/replay-knowledge",
        tags=["test-only"],
    )

    @router.get("/provider")
    async def provider_state() -> dict[str, int | bool]:
        return state.snapshot()

    @router.post("/provider/faults")
    async def set_provider_faults(
        faults: ProviderFaults,
    ) -> dict[str, int | bool]:
        """Replace the whole fault-injection state with the posted values."""

        state.replace_faults(
            embedding_failures=faults.embedding_failures,
            rerank_failures=faults.rerank_failures,
            chat_failures=faults.chat_failures,
            embedding_blocked=faults.embedding_blocked,
        )
        return state.snapshot()

    @router.get("/objects")
    async def bucket_objects() -> dict[str, list[str]]:
        return {"keys": await asyncio.to_thread(list_objects)}

    @router.get("/storage")
    async def storage_state() -> dict[str, int]:
        return await asyncio.to_thread(storage_control.storage_snapshot)

    @router.post("/storage/faults")
    async def set_storage_faults(faults: StorageFaults) -> dict[str, int]:
        await asyncio.to_thread(
            storage_control.replace_faults,
            source_put_failures=faults.source_put_failures,
            manifest_put_failures=faults.manifest_put_failures,
            attachment_put_failures=faults.attachment_put_failures,
            delete_failures=faults.delete_failures,
        )
        return storage_control.storage_snapshot()

    @router.get("/projects/{project_id}/facts")
    async def project_facts(project_id: uuid.UUID) -> dict[str, int]:
        session_factory = get_session_factory()
        async with session_factory() as session:
            project = await session.get(ProjectRow, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="Not Found")
            document_rows = int(await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id)) or 0)
            published_documents = int(
                await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.project_id == project_id,
                        KnowledgeDocumentRow.published_version.is_not(None),
                    )
                )
                or 0
            )
            extraction_rows = int(await session.scalar(select(func.count()).select_from(KnowledgeExtractionRow).where(KnowledgeExtractionRow.project_id == project_id)) or 0)
            ready_attachments = int(
                await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeAttachmentRow)
                    .where(
                        KnowledgeAttachmentRow.project_id == project_id,
                        KnowledgeAttachmentRow.state == "ready",
                    )
                )
                or 0
            )
            open_tasks = int(
                await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeTaskRow)
                    .where(
                        KnowledgeTaskRow.project_id == project_id,
                        KnowledgeTaskRow.status.in_(("queued", "running", "retry_wait")),
                    )
                )
                or 0
            )
            running_tasks = int(
                await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeTaskRow)
                    .where(
                        KnowledgeTaskRow.project_id == project_id,
                        KnowledgeTaskRow.status == "running",
                    )
                )
                or 0
            )
            quota = await session.scalar(
                select(ProjectUsageCounterRow).where(
                    ProjectUsageCounterRow.project_id == project_id,
                    ProjectUsageCounterRow.dimension == "storage_bytes",
                    ProjectUsageCounterRow.bucket == "lifetime",
                )
            )
        prefix = f"projects/{project_id}/knowledge/"
        object_count = sum(key.startswith(prefix) for key in await asyncio.to_thread(list_objects))
        return {
            "object_count": object_count,
            "document_rows": document_rows,
            "published_documents": published_documents,
            "extraction_rows": extraction_rows,
            "ready_attachments": ready_attachments,
            "open_tasks": open_tasks,
            "running_tasks": running_tasks,
            "quota_used": int(quota.used if quota is not None else 0),
            "quota_reserved": int(quota.reserved if quota is not None else 0),
        }

    @router.post("/projects/{project_id}/authority")
    async def set_project_authority(
        project_id: uuid.UUID,
        fault: ProjectAuthorityFault,
    ) -> dict[str, bool]:
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            project = await session.get(ProjectRow, project_id, with_for_update=True)
            if project is None:
                raise HTTPException(status_code=404, detail="Not Found")
            project.status = "pending_deletion" if fault.revoked else "active"
            project.is_suspended = fault.revoked
            project.membership_version += 1
        return {"revoked": fault.revoked}

    return router
