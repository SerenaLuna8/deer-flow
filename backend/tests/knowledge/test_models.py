"""M2 gates: SiliconFlow client behavior and model-configuration service rules.

Client tests speak to a mock transport that replays real SiliconFlow response
shapes. Service tests run against the installed Schema V1 snapshot so the
uniqueness, foreign-key, and in-use rules exercised here match production.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_RERANK_FAILED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
    KnowledgeModelConfigurationCreate,
    KnowledgeModelConfigurationUpdate,
    KnowledgeProtectedSecret,
)
from actweave_knowledge.models.client import (
    EMBEDDING_PROBE_TEXT,
    PROBE_TIMEOUT_SECONDS_CAP,
    RERANK_PROBE_DOCUMENTS,
    RERANK_PROBE_QUERY,
    KnowledgeModelClient,
    KnowledgeModelMaterial,
    RerankScore,
)
from actweave_knowledge.models.service import KnowledgeModelConfigurationService
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeModelConfigurationRow,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from deerflow.persistence.bootstrap import _install_full_schema

# ---------------------------------------------------------------------------
# Client fixtures
# ---------------------------------------------------------------------------


def _material(**overrides: object) -> KnowledgeModelMaterial:
    values: dict[str, object] = {
        "configuration_id": uuid.uuid4(),
        "base_url": "https://provider.invalid/v1",
        "embedding_model": "embed-model",
        "embedding_dimension": 3,
        "embedding_max_batch": 2,
        "reranker_model": "rerank-model",
        "reranker_max_batch": 2,
        "request_timeout_seconds": 7,
        "api_key": "material-secret-key",
    }
    values.update(overrides)
    return KnowledgeModelMaterial(**values)  # type: ignore[arg-type]


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.headers: list[httpx.Headers] = []
        self.timeouts: list[object] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append((request.url.path, json.loads(request.content)))
        self.headers.append(request.headers)
        self.timeouts.append(request.extensions.get("timeout"))


def _client(handler) -> KnowledgeModelClient:  # noqa: ANN001 - httpx handler
    return KnowledgeModelClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _embedding_response(indexed_vectors: list[tuple[int, list[float]]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"index": index, "embedding": vector} for index, vector in indexed_vectors]},
    )


def _rerank_response(results: list[tuple[int, float]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": [{"index": index, "relevance_score": score} for index, score in results]},
    )


# ---------------------------------------------------------------------------
# Client: embedding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_restores_provider_index_order_and_sends_contract_payload() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        return _embedding_response([(1, [2.0, 0.0, 0.0]), (0, [1.0, 0.0, 0.0])])

    material = _material()
    client = _client(handler)
    try:
        vectors = await client.embed(material, ["first", "second"])
    finally:
        await client.aclose()

    assert vectors == [[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    path, body = recorder.requests[0]
    assert path == "/v1/embeddings"
    assert body == {
        "model": "embed-model",
        "input": ["first", "second"],
        "dimensions": 3,
        "encoding_format": "float",
    }
    assert recorder.headers[0]["authorization"] == "Bearer material-secret-key"
    timeout = recorder.timeouts[0]
    assert timeout is not None and set(timeout.values()) == {7.0}


@pytest.mark.asyncio
async def test_embed_batches_by_configuration_and_keeps_global_order() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        batch = json.loads(request.content)["input"]
        # Answer each batch in reverse index order to prove re-ordering.
        return _embedding_response([(index, [float(text_value.removeprefix("text-")) + 1.0, 1.0, 0.0]) for index, text_value in reversed(list(enumerate(batch)))])

    texts = [f"text-{position}" for position in range(5)]
    client = _client(handler)
    try:
        vectors = await client.embed(_material(), texts)
    finally:
        await client.aclose()

    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [body["input"] for _, body in recorder.requests] == [
        ["text-0", "text-1"],
        ["text-2", "text-3"],
        ["text-4"],
    ]


@pytest.mark.asyncio
async def test_embed_accepts_siliconflow_shard_reset_indexes() -> None:
    """Regression: SiliconFlow numbers ``index`` per internal shard of 8.

    A 15-text batch really returned indexes ``0..7, 0..6`` and parked the
    document as failed; the array order is the input order and must win.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)["input"]
        return _embedding_response([(position % 8, [float(text_value.removeprefix("text-")) + 1.0, 1.0, 0.0]) for position, text_value in enumerate(batch)])

    texts = [f"text-{position}" for position in range(15)]
    client = _client(handler)
    try:
        vectors = await client.embed(_material(embedding_max_batch=64), texts)
    finally:
        await client.aclose()

    assert [vector[0] for vector in vectors] == [float(position) + 1.0 for position in range(15)]


@pytest.mark.asyncio
async def test_embed_with_no_texts_never_calls_the_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider must not be called")

    client = _client(handler)
    try:
        assert await client.embed(_material(), []) == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable_status", [429, 500, 503])
async def test_embed_retries_once_on_retryable_statuses(retryable_status: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(retryable_status)
        return _embedding_response([(0, [1.0, 0.0, 0.0])])

    client = _client(handler)
    try:
        vectors = await client.embed(_material(), ["only"])
    finally:
        await client.aclose()

    assert attempts == 2
    assert vectors == [[1.0, 0.0, 0.0]]


@pytest.mark.asyncio
async def test_embed_recovers_when_the_retry_succeeds_after_a_transport_error() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("first attempt drops")
        return _embedding_response([(0, [1.0, 0.0, 0.0])])

    client = _client(handler)
    try:
        vectors = await client.embed(_material(), ["only"])
    finally:
        await client.aclose()

    assert attempts == 2
    assert vectors == [[1.0, 0.0, 0.0]]


@pytest.mark.asyncio
async def test_embed_fails_after_second_retryable_status_without_third_attempt() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502)

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.embed(_material(), ["only"])
    finally:
        await client.aclose()

    assert attempts == 2
    assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED


@pytest.mark.asyncio
async def test_embed_does_not_retry_plain_client_errors() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401)

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.embed(_material(), ["only"])
    finally:
        await client.aclose()

    assert attempts == 1
    assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")],
    ids=["connect-error", "timeout"],
)
async def test_embed_maps_exhausted_transport_failures_to_model_unavailable(
    transport_error: Exception,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise transport_error

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.embed(_material(), ["only"])
    finally:
        await client.aclose()

    assert attempts == 2
    assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]},  # count mismatch
        {
            "data": [
                {"index": 1, "embedding": [1.0, 0.0, 0.0]},  # neither permutation
                {"index": 1, "embedding": [2.0, 0.0, 0.0]},  # nor shard reset
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                {"index": 2, "embedding": [2.0, 0.0, 0.0]},  # index out of range
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [1.0, 0.0]},  # wrong dimension
                {"index": 1, "embedding": [2.0, 0.0, 0.0]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [float("nan"), 0.0, 0.0]},
                {"index": 1, "embedding": [2.0, 0.0, 0.0]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [0.0, 0.0, 0.0]},  # all-zero vector
                {"index": 1, "embedding": [2.0, 0.0, 0.0]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [True, False, True]},  # booleans
                {"index": 1, "embedding": [2.0, 0.0, 0.0]},
            ]
        },
        {"unexpected": []},  # missing data
    ],
    ids=[
        "count-mismatch",
        "unrecoverable-order",
        "index-out-of-range",
        "wrong-dimension",
        "nan-value",
        "all-zero-vector",
        "boolean-values",
        "missing-data",
    ],
)
async def test_embed_rejects_invalid_provider_payloads(payload: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.embed(_material(), ["first", "second"])
    finally:
        await client.aclose()

    assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED


# ---------------------------------------------------------------------------
# Client: rerank
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_maps_indexes_and_sends_contract_payload() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        return _rerank_response([(1, 0.9), (0, 0.2)])

    client = _client(handler)
    try:
        scores = await client.rerank(_material(), "the query", ["doc a", "doc b"], top_n=2)
    finally:
        await client.aclose()

    assert scores == [RerankScore(index=1, score=0.9), RerankScore(index=0, score=0.2)]
    path, body = recorder.requests[0]
    assert path == "/v1/rerank"
    assert body == {
        "model": "rerank-model",
        "query": "the query",
        "documents": ["doc a", "doc b"],
        "top_n": 2,
        "return_documents": False,
    }


@pytest.mark.asyncio
async def test_rerank_with_no_documents_never_calls_the_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider must not be called")

    client = _client(handler)
    try:
        assert await client.rerank(_material(), "query", [], top_n=4) == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rerank_batches_offset_mapping_and_cross_batch_merge() -> None:
    recorder = _Recorder()
    responses = iter(
        [
            _rerank_response([(0, 0.5), (1, 0.4)]),
            _rerank_response([(0, 0.9), (1, 0.1)]),
            _rerank_response([(0, 0.7)]),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        return next(responses)

    documents = [f"candidate-{position}" for position in range(5)]
    client = _client(handler)
    try:
        scores = await client.rerank(_material(), "query", documents, top_n=3)
    finally:
        await client.aclose()

    # Batch-local indexes map through offsets 0/2/4; merge sorts by score.
    assert scores == [
        RerankScore(index=2, score=0.9),
        RerankScore(index=4, score=0.7),
        RerankScore(index=0, score=0.5),
    ]
    assert [(body["documents"], body["top_n"]) for _, body in recorder.requests] == [
        (["candidate-0", "candidate-1"], 2),
        (["candidate-2", "candidate-3"], 2),
        (["candidate-4"], 1),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}],  # duplicate
        [{"index": 5, "relevance_score": 0.9}],  # out of range
        [{"index": 0, "relevance_score": float("inf")}],  # non-finite
        [{"index": 0, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.9}],  # ascending order
        [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
            {"index": 1, "relevance_score": 0.7},
        ],  # more results than top_n
    ],
    ids=["duplicate-index", "out-of-range", "non-finite-score", "unsorted", "too-many-results"],
)
async def test_rerank_rejects_invalid_provider_payloads(results: list[dict[str, object]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"results": results}),
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.rerank(_material(), "query", ["doc a", "doc b"], top_n=2)
    finally:
        await client.aclose()

    assert error.value.code == KNOWLEDGE_RERANK_FAILED


@pytest.mark.asyncio
async def test_rerank_missing_results_key_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.rerank(_material(), "query", ["doc a"], top_n=1)
    finally:
        await client.aclose()

    assert error.value.code == KNOWLEDGE_RERANK_FAILED


@pytest.mark.asyncio
async def test_rerank_retries_once_then_fails_with_rerank_code() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500)

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.rerank(_material(), "query", ["doc a"], top_n=1)
    finally:
        await client.aclose()

    assert attempts == 2
    assert error.value.code == KNOWLEDGE_RERANK_FAILED


# ---------------------------------------------------------------------------
# Client: connection probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_connection_probes_both_endpoints_with_fixed_texts() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        if request.url.path.endswith("/embeddings"):
            return _embedding_response([(0, [1.0, 0.5, 0.0])])
        return _rerank_response([(0, 0.8), (1, 0.3)])

    client = _client(handler)
    try:
        await client.verify_connection(_material())
    finally:
        await client.aclose()

    assert [path for path, _ in recorder.requests] == ["/v1/embeddings", "/v1/rerank"]
    embed_body = recorder.requests[0][1]
    rerank_body = recorder.requests[1][1]
    assert embed_body["input"] == [EMBEDDING_PROBE_TEXT]
    assert rerank_body["query"] == RERANK_PROBE_QUERY
    assert rerank_body["documents"] == list(RERANK_PROBE_DOCUMENTS)
    assert rerank_body["top_n"] == 2


@pytest.mark.asyncio
async def test_verify_connection_requires_a_rerank_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return _embedding_response([(0, [1.0, 0.5, 0.0])])
        return _rerank_response([])

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.verify_connection(_material())
    finally:
        await client.aclose()

    assert error.value.code == KNOWLEDGE_RERANK_FAILED


@pytest.mark.asyncio
async def test_verify_connection_clamps_the_probe_timeout() -> None:
    """A 300s production timeout must not pin the admin request (or the
    update flow's row lock) during a connection probe."""

    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        if request.url.path.endswith("/embeddings"):
            return _embedding_response([(0, [1.0, 0.5, 0.0])])
        return _rerank_response([(0, 0.8), (1, 0.3)])

    client = _client(handler)
    try:
        await client.verify_connection(_material(request_timeout_seconds=300))
    finally:
        await client.aclose()

    for timeout in recorder.timeouts:
        assert timeout is not None and set(timeout.values()) == {float(PROBE_TIMEOUT_SECONDS_CAP)}


def test_default_http_client_never_trusts_ambient_proxies() -> None:
    client = KnowledgeModelClient()
    assert client._http.trust_env is False
    assert client._http.follow_redirects is False


# ---------------------------------------------------------------------------
# Service fixtures
# ---------------------------------------------------------------------------


class _MemorySecretPort:
    def __init__(self) -> None:
        self.protect_calls: list[tuple[uuid.UUID, str]] = []

    def protect_api_key(self, configuration_id: uuid.UUID, api_key: str) -> KnowledgeProtectedSecret:
        self.protect_calls.append((configuration_id, api_key))
        payload = f"{configuration_id}:{api_key}".encode()
        return KnowledgeProtectedSecret(nonce=b"n" * 12, ciphertext=payload.ljust(16, b"\0"))

    def materialize_api_key(self, configuration_id: uuid.UUID, secret: KnowledgeProtectedSecret) -> str:
        decoded = secret.ciphertext.rstrip(b"\0").decode()
        prefix = f"{configuration_id}:"
        if not decoded.startswith(prefix):
            raise ValueError("secret bound to another configuration")
        return decoded[len(prefix) :]


class _FailingSecretPort(_MemorySecretPort):
    def protect_api_key(self, configuration_id: uuid.UUID, api_key: str) -> KnowledgeProtectedSecret:
        raise RuntimeError("encryption backend down")


class _StubModelClient:
    def __init__(self, error: KnowledgeError | None = None) -> None:
        self.error = error
        self.materials: list[KnowledgeModelMaterial] = []

    async def verify_connection(self, material: KnowledgeModelMaterial) -> None:
        self.materials.append(material)
        if self.error is not None:
            raise self.error


def _create(**overrides: object) -> KnowledgeModelConfigurationCreate:
    values: dict[str, object] = {
        "display_name": "Retrieval",
        "base_url": "https://provider.invalid/v1",
        "embedding_model": "embed-model",
        "embedding_dimension": 1024,
        "reranker_model": "rerank-model",
        "api_key": "plain-api-key",
    }
    values.update(overrides)
    return KnowledgeModelConfigurationCreate(**values)  # type: ignore[arg-type]


async def _seed_project(session: AsyncSession, label: str) -> uuid.UUID:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :user_id, :email, :username, 'user', now(), false, 1
               )"""
        ),
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m2_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m2-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _reference_with_base(factory: async_sessionmaker[AsyncSession], configuration_id: uuid.UUID) -> None:
    async with factory() as session, session.begin():
        project_id = await _seed_project(session, f"ref{uuid.uuid4().hex[:8]}")
        session.add(
            KnowledgeBaseRow(
                id=uuid.uuid4(),
                project_id=project_id,
                name=f"Base {uuid.uuid4().hex[:8]}",
                model_configuration_id=configuration_id,
            )
        )


class _ServiceHarness:
    def __init__(self, engine, factory, service, client, secret_port) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.service = service
        self.client = client
        self.secret_port = secret_port


async def _harness(
    postgres_database_url: str,
    *,
    client: _StubModelClient | None = None,
    secret_port: _MemorySecretPort | None = None,
) -> _ServiceHarness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    stub_client = client or _StubModelClient()
    port = secret_port or _MemorySecretPort()
    service = KnowledgeModelConfigurationService(
        session_factory=factory,
        secret_port=port,
        client=stub_client,  # type: ignore[arg-type]
    )
    return _ServiceHarness(engine, factory, service, stub_client, port)


# ---------------------------------------------------------------------------
# Service: create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_verifies_connection_then_persists_encrypted_key(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        view = await harness.service.create_model_configuration(_create())

        assert view.display_name == "Retrieval"
        assert view.status == "active"
        assert view.in_use is False
        material = harness.client.materials[0]
        assert material.api_key == "plain-api-key"
        assert material.base_url == "https://provider.invalid/v1"
        async with harness.factory() as session:
            row = await session.get(KnowledgeModelConfigurationRow, view.id)
            assert row is not None
            assert b"plain-api-key" != row.api_key_ciphertext
            assert (
                harness.secret_port.materialize_api_key(
                    view.id,
                    KnowledgeProtectedSecret(nonce=row.api_key_nonce, ciphertext=row.api_key_ciphertext),
                )
                == "plain-api-key"
            )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_failing_connection_test_leaves_no_configuration(
    postgres_database_url: str,
) -> None:
    failing = _StubModelClient(error=KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "embedding broken"))
    harness = await _harness(postgres_database_url, client=failing)
    try:
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_model_configuration(_create())
        assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED
        async with harness.factory() as session:
            count = await session.scalar(select(func.count()).select_from(KnowledgeModelConfigurationRow))
            assert count == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_encryption_failure_leaves_no_configuration(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url, secret_port=_FailingSecretPort())
    try:
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_model_configuration(_create())
        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        async with harness.factory() as session:
            count = await session.scalar(select(func.count()).select_from(KnowledgeModelConfigurationRow))
            assert count == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_rejects_case_insensitive_duplicate_names(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        await harness.service.create_model_configuration(_create(display_name="Retrieval"))
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_model_configuration(_create(display_name="retrieval"))
        assert error.value.code == KNOWLEDGE_NAME_CONFLICT
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"display_name": "   "},
        {"display_name": "x" * 121},
        {"base_url": "ftp://provider.invalid"},
        {"base_url": "not-a-url"},
        {"base_url": "https://provider.invalid/v1?tenant=1"},
        {"base_url": "https://provider.invalid/v1#frag"},
        {"base_url": "https://user:pass@provider.invalid/v1"},
        {"embedding_model": ""},
        {"embedding_dimension": 0},
        {"embedding_dimension": 16001},
        {"embedding_max_batch": 0},
        {"embedding_max_batch": 2049},
        {"reranker_model": " "},
        {"reranker_max_batch": 257},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": 301},
        {"api_key": ""},
    ],
    ids=[
        "blank-name",
        "long-name",
        "ftp-url",
        "not-a-url",
        "url-with-query",
        "url-with-fragment",
        "url-with-userinfo",
        "empty-embedding-model",
        "dimension-low",
        "dimension-high",
        "embedding-batch-low",
        "embedding-batch-high",
        "blank-reranker-model",
        "reranker-batch-high",
        "timeout-low",
        "timeout-high",
        "empty-api-key",
    ],
)
async def test_create_validates_fields_before_any_provider_call(
    postgres_database_url: str,
    overrides: dict[str, object],
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_model_configuration(_create(**overrides))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert harness.client.materials == []
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Service: list and options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_paginates_in_creation_order_and_derives_in_use(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        base_time = datetime(2036, 3, 1, 12, 0, tzinfo=UTC)
        ids: list[uuid.UUID] = []
        async with harness.factory() as session, session.begin():
            for position in range(3):
                configuration_id = uuid.uuid4()
                ids.append(configuration_id)
                session.add(
                    KnowledgeModelConfigurationRow(
                        id=configuration_id,
                        display_name=f"Configuration {position}",
                        status="active",
                        base_url="https://provider.invalid/v1",
                        embedding_model="embed-model",
                        embedding_dimension=1024,
                        embedding_max_batch=64,
                        reranker_model="rerank-model",
                        reranker_max_batch=32,
                        request_timeout_seconds=30,
                        api_key_nonce=b"n" * 12,
                        api_key_ciphertext=b"c" * 16,
                        created_at=base_time + timedelta(minutes=position),
                        updated_at=base_time + timedelta(minutes=position),
                    )
                )
        await _reference_with_base(harness.factory, ids[1])

        first_page, total = await harness.service.list_model_configurations(page=1, page_size=2)
        second_page, _ = await harness.service.list_model_configurations(page=2, page_size=2)

        assert total == 3
        assert [view.display_name for view in first_page] == ["Configuration 0", "Configuration 1"]
        assert [view.display_name for view in second_page] == ["Configuration 2"]
        assert [view.in_use for view in first_page] == [False, True]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_list_rejects_out_of_range_pagination(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_model_configurations(page=0)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        with pytest.raises(KnowledgeError):
            await harness.service.list_model_configurations(page_size=101)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_model_options_return_only_active_configurations(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        active = await harness.service.create_model_configuration(_create(display_name="Bravo"))
        await harness.service.create_model_configuration(_create(display_name="Alpha"))
        disabled = await harness.service.create_model_configuration(_create(display_name="Charlie"))
        await harness.service.update_model_configuration(
            disabled.id,
            KnowledgeModelConfigurationUpdate(status="disabled"),
        )

        options = await harness.service.list_active_model_options()

        assert [option.display_name for option in options] == ["Alpha", "Bravo"]
        chosen = next(option for option in options if option.display_name == "Bravo")
        assert chosen.id == active.id
        assert chosen.embedding_model == "embed-model"
        assert chosen.embedding_dimension == 1024
        assert chosen.reranker_model == "rerank-model"
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Service: update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plain_fields_skips_retest_and_bumps_updated_at(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        harness.client.materials.clear()

        updated = await harness.service.update_model_configuration(
            created.id,
            KnowledgeModelConfigurationUpdate(
                display_name="Renamed",
                embedding_max_batch=16,
                reranker_max_batch=8,
                request_timeout_seconds=60,
            ),
        )

        assert harness.client.materials == []
        assert updated.display_name == "Renamed"
        assert updated.embedding_max_batch == 16
        assert updated.reranker_max_batch == 8
        assert updated.request_timeout_seconds == 60
        assert updated.updated_at > created.updated_at
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_without_changes_is_a_read_only_no_op(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        harness.client.materials.clear()

        unchanged = await harness.service.update_model_configuration(
            created.id,
            KnowledgeModelConfigurationUpdate(),
        )

        assert harness.client.materials == []
        assert unchanged.updated_at == created.updated_at
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_api_key_retests_with_new_key_and_replaces_secret(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        harness.client.materials.clear()

        await harness.service.update_model_configuration(
            created.id,
            KnowledgeModelConfigurationUpdate(api_key="rotated-api-key"),
        )

        assert [material.api_key for material in harness.client.materials] == ["rotated-api-key"]
        async with harness.factory() as session:
            row = await session.get(KnowledgeModelConfigurationRow, created.id)
            assert row is not None
            assert (
                harness.secret_port.materialize_api_key(
                    created.id,
                    KnowledgeProtectedSecret(nonce=row.api_key_nonce, ciphertext=row.api_key_ciphertext),
                )
                == "rotated-api-key"
            )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_semantic_fields_retests_with_stored_key_when_unreferenced(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        harness.client.materials.clear()

        updated = await harness.service.update_model_configuration(
            created.id,
            KnowledgeModelConfigurationUpdate(
                base_url="https://other-provider.invalid/v2",
                embedding_model="embed-next",
                embedding_dimension=2048,
                reranker_model="rerank-next",
            ),
        )

        material = harness.client.materials[0]
        assert material.api_key == "plain-api-key"
        assert material.base_url == "https://other-provider.invalid/v2"
        assert material.embedding_model == "embed-next"
        assert material.embedding_dimension == 2048
        assert material.reranker_model == "rerank-next"
        assert updated.base_url == "https://other-provider.invalid/v2"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_failing_retest_keeps_stored_configuration(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        harness.client.error = KnowledgeError(KNOWLEDGE_RERANK_FAILED, "rerank broken")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_model_configuration(
                created.id,
                KnowledgeModelConfigurationUpdate(embedding_model="embed-next"),
            )
        assert error.value.code == KNOWLEDGE_RERANK_FAILED
        views, _ = await harness.service.list_model_configurations()
        assert views[0].embedding_model == "embed-model"
    finally:
        await harness.engine.dispose()


class _RaceOnProbeClient(_StubModelClient):
    """After a successful probe, mutate the row like a concurrent admin would."""

    def __init__(self) -> None:
        super().__init__()
        self.factory: async_sessionmaker[AsyncSession] | None = None
        self.race_configuration_id: uuid.UUID | None = None

    async def verify_connection(self, material: KnowledgeModelMaterial) -> None:
        await super().verify_connection(material)
        if self.factory is None or self.race_configuration_id is None:
            return
        async with self.factory() as session, session.begin():
            row = await session.get(KnowledgeModelConfigurationRow, self.race_configuration_id)
            assert row is not None
            row.embedding_max_batch = 128
            row.updated_at = func.now()  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_update_rejects_stale_probe_after_concurrent_change(
    postgres_database_url: str,
) -> None:
    """The probe runs outside the row lock; settle re-locks and rejects stale material."""

    client = _RaceOnProbeClient()
    harness = await _harness(postgres_database_url, client=client)
    try:
        created = await harness.service.create_model_configuration(_create())
        client.factory = harness.factory
        client.race_configuration_id = created.id

        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_model_configuration(
                created.id,
                KnowledgeModelConfigurationUpdate(embedding_model="embed-next"),
            )
        assert error.value.code == KNOWLEDGE_CONFLICT

        views, _ = await harness.service.list_model_configurations()
        assert views[0].embedding_model == "embed-model", "stale probe must not certify the update"
        assert views[0].embedding_max_batch == 128, "the concurrent write must survive"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_referenced_configuration_freezes_retrieval_semantics(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        await _reference_with_base(harness.factory, created.id)
        harness.client.materials.clear()

        for update in (
            KnowledgeModelConfigurationUpdate(status="disabled"),
            KnowledgeModelConfigurationUpdate(base_url="https://other.invalid/v1"),
            KnowledgeModelConfigurationUpdate(embedding_model="embed-next"),
            KnowledgeModelConfigurationUpdate(embedding_dimension=2048),
            KnowledgeModelConfigurationUpdate(reranker_model="rerank-next"),
        ):
            with pytest.raises(KnowledgeError) as error:
                await harness.service.update_model_configuration(created.id, update)
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert harness.client.materials == []

        renamed = await harness.service.update_model_configuration(
            created.id,
            KnowledgeModelConfigurationUpdate(display_name="Still Editable", api_key="rotated"),
        )
        assert renamed.display_name == "Still Editable"
        assert renamed.in_use is True
        assert [material.api_key for material in harness.client.materials] == ["rotated"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_rejects_unknown_configuration_and_name_conflicts(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        await harness.service.create_model_configuration(_create(display_name="First"))
        second = await harness.service.create_model_configuration(_create(display_name="Second"))

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.update_model_configuration(
                uuid.uuid4(),
                KnowledgeModelConfigurationUpdate(display_name="Anything"),
            )
        assert missing.value.code == KNOWLEDGE_NOT_FOUND

        with pytest.raises(KnowledgeError) as conflict:
            await harness.service.update_model_configuration(
                second.id,
                KnowledgeModelConfigurationUpdate(display_name="first"),
            )
        assert conflict.value.code == KNOWLEDGE_NAME_CONFLICT
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Service: delete and test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_unreferenced_configuration_and_its_secret(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())

        await harness.service.delete_model_configuration(created.id)

        async with harness.factory() as session:
            assert await session.get(KnowledgeModelConfigurationRow, created.id) is None
        with pytest.raises(KnowledgeError) as error:
            await harness.service.delete_model_configuration(created.id)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_referenced_configuration_is_rejected(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        await _reference_with_base(harness.factory, created.id)

        with pytest.raises(KnowledgeError) as error:
            await harness.service.delete_model_configuration(created.id)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        async with harness.factory() as session:
            assert await session.get(KnowledgeModelConfigurationRow, created.id) is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_connection_test_uses_stored_key_and_reports_failures_as_result(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        created = await harness.service.create_model_configuration(_create())
        harness.client.materials.clear()

        succeeded = await harness.service.test_model_configuration(created.id)
        assert succeeded.ok is True
        assert harness.client.materials[0].api_key == "plain-api-key"

        harness.client.error = KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "无法连接模型服务或请求超时")
        failed = await harness.service.test_model_configuration(created.id)
        assert failed.ok is False
        assert failed.message == "无法连接模型服务或请求超时"

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.test_model_configuration(uuid.uuid4())
        assert missing.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()
