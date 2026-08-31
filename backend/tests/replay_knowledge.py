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

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

_MINIO_ENDPOINT_ENV = "ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT"
_MINIO_ACCESS_KEY_ENV = "ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY"
_MINIO_SECRET_KEY_ENV = "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY"

_REPLAY_KNOWLEDGE_NAMESPACE = uuid.UUID("9a1de0f4-63b7-5c58-9f2e-0d84a3c14b72")

REPLAY_KNOWLEDGE_MODEL_DISPLAY_NAME = "Replay Knowledge Model"
REPLAY_KNOWLEDGE_MODEL_API_KEY = "sk-replay-knowledge-mock"
REPLAY_EMBEDDING_DIMENSION = 64

# Documents containing this exact substring get the far-from-query embedding
# (cosine ranks them last) but the top rerank score (final order ranks them
# first). Specs must keep the marker out of their queries.
DOC_RERANK_MARKER = "深海列车"
RERANK_MARKER_SCORE = 0.95


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


def build_knowledge_config_block(*, bucket: str) -> str:
    """The ``knowledge`` block appended to the replay process config.

    Endpoint and credentials stay env-substituted so they never land in the
    temporary config file; the bucket name is run-local and not a secret.
    """

    return f"""\
knowledge:
  enabled: true
  worker_concurrency: 2
  task_timeout_seconds: 120
  upload_max_bytes: 10485760
  minio:
    endpoint: ${_MINIO_ENDPOINT_ENV}
    bucket: {bucket}
    access_key: ${_MINIO_ACCESS_KEY_ENV}
    secret_key: ${_MINIO_SECRET_KEY_ENV}
    secure: false
"""


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
    embedding_failures_remaining: int = 0
    rerank_failures_remaining: int = 0

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "embedding_calls": self.embedding_calls,
                "rerank_calls": self.rerank_calls,
                "embedding_failures_remaining": self.embedding_failures_remaining,
                "rerank_failures_remaining": self.rerank_failures_remaining,
            }


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

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        with state.lock:
            state.embedding_calls += 1
            if state.embedding_failures_remaining > 0:
                state.embedding_failures_remaining -= 1
                return JSONResponse({"error": "replay embedding fault"}, status_code=500)
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
):
    """Build the ``/api/test-only/replay-knowledge`` control API."""

    import asyncio

    from fastapi import APIRouter
    from pydantic import BaseModel, ConfigDict, Field

    class ProviderFaults(BaseModel):
        model_config = ConfigDict(extra="forbid")

        embedding_failures: int = Field(default=0, ge=0, le=1000)
        rerank_failures: int = Field(default=0, ge=0, le=1000)

    router = APIRouter(
        prefix="/api/test-only/replay-knowledge",
        tags=["test-only"],
    )

    @router.get("/provider")
    async def provider_state() -> dict[str, int]:
        return state.snapshot()

    @router.post("/provider/faults")
    async def set_provider_faults(faults: ProviderFaults) -> dict[str, int]:
        """Replace the whole fault-injection state with the posted values."""

        with state.lock:
            state.embedding_failures_remaining = faults.embedding_failures
            state.rerank_failures_remaining = faults.rerank_failures
        return state.snapshot()

    @router.get("/objects")
    async def bucket_objects() -> dict[str, list[str]]:
        return {"keys": await asyncio.to_thread(list_objects)}

    return router
