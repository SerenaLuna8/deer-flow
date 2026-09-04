"""M2 gates: SiliconFlow client behavior for typed embedding/rerank materials.

Client tests speak to a mock transport that replays real SiliconFlow response
shapes, covering batching, ordering, retry, and payload validation. Model
configuration CRUD moved to the host registry in M9; its rules are tested in
``tests/model_registry/``.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_RERANK_FAILED,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeRerankMaterial,
)
from actweave_knowledge.models.client import (
    PROBE_TIMEOUT_SECONDS_CAP,
    KnowledgeModelClient,
    RerankScore,
)

# ---------------------------------------------------------------------------
# Client fixtures
# ---------------------------------------------------------------------------


def _embedding_material(**overrides: object) -> KnowledgeEmbeddingMaterial:
    values: dict[str, object] = {
        "model_id": uuid.uuid4(),
        "base_url": "https://provider.invalid/v1",
        "model_name": "embed-model",
        "dimension": 3,
        "max_batch": 2,
        "request_timeout_seconds": 7,
        "api_key": "material-secret-key",
    }
    values.update(overrides)
    return KnowledgeEmbeddingMaterial(**values)  # type: ignore[arg-type]


def _rerank_material(**overrides: object) -> KnowledgeRerankMaterial:
    values: dict[str, object] = {
        "model_id": uuid.uuid4(),
        "base_url": "https://provider.invalid/v1",
        "model_name": "rerank-model",
        "max_batch": 2,
        "request_timeout_seconds": 7,
        "api_key": "material-secret-key",
    }
    values.update(overrides)
    return KnowledgeRerankMaterial(**values)  # type: ignore[arg-type]


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

    material = _embedding_material()
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
        vectors = await client.embed(_embedding_material(max_batch=64), texts)
    finally:
        await client.aclose()

    assert [vector[0] for vector in vectors] == [float(position) + 1.0 for position in range(15)]


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable_status", [429, 503])
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
        vectors = await client.embed(_embedding_material(), ["only"])
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
            await client.embed(_embedding_material(), ["only"])
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
            await client.embed(_embedding_material(), ["only"])
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
            await client.embed(_embedding_material(), ["only"])
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
            await client.embed(_embedding_material(), ["first", "second"])
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
        scores = await client.rerank(_rerank_material(), "the query", ["doc a", "doc b"], top_n=2)
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
        scores = await client.rerank(_rerank_material(), "query", documents, top_n=3)
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
        # Fewer results than requested: a provider that truncates or filters
        # internally would silently drop candidates before thresholds run.
        [{"index": 0, "relevance_score": 0.9}],
    ],
    ids=["duplicate-index", "out-of-range", "non-finite-score", "unsorted", "too-many-results", "fewer-results"],
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
            await client.rerank(_rerank_material(), "query", ["doc a", "doc b"], top_n=2)
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
            await client.rerank(_rerank_material(), "query", ["doc a"], top_n=1)
    finally:
        await client.aclose()

    assert attempts == 2
    assert error.value.code == KNOWLEDGE_RERANK_FAILED


# ---------------------------------------------------------------------------
# Client: typed connection probes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_probes_clamp_the_timeout() -> None:
    """A 300s production timeout must not pin the admin request (or the
    registry update flow's row lock) during a connection probe."""

    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        if request.url.path.endswith("/embeddings"):
            return _embedding_response([(0, [1.0, 0.5, 0.0])])
        return _rerank_response([(0, 0.8), (1, 0.3)])

    client = _client(handler)
    try:
        await client.verify_embedding(_embedding_material(request_timeout_seconds=300))
        await client.verify_rerank(_rerank_material(request_timeout_seconds=300))
    finally:
        await client.aclose()

    assert len(recorder.timeouts) == 2
    for timeout in recorder.timeouts:
        assert timeout is not None and set(timeout.values()) == {float(PROBE_TIMEOUT_SECONDS_CAP)}


def test_default_http_client_never_trusts_ambient_proxies() -> None:
    client = KnowledgeModelClient()
    assert client._http.trust_env is False
    assert client._http.follow_redirects is False


# ---------------------------------------------------------------------------
# Client: per-batch guard and verified-batch progress hooks (M10 T4)
# ---------------------------------------------------------------------------


class _HookLog:
    """Order-preserving record of guard runs, HTTP dispatches, and progress."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail_at_guard_run: int | None = None
        self._guard_runs = 0

    async def guard(self) -> None:
        self._guard_runs += 1
        self.events.append(f"guard:{self._guard_runs}")
        if self.fail_at_guard_run == self._guard_runs:
            raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "授权或租约已失效")

    async def verified(self, count: int) -> None:
        self.events.append(f"verified:{count}")


@pytest.mark.asyncio
async def test_embed_runs_the_batch_guard_before_every_dispatch_and_before_the_retry() -> None:
    """max_batch=2 over three texts is two provider batches; the second batch's
    first attempt gets a 429, so its retry must re-run the guard first. The
    verified hook reports exactly the validated batch sizes, in order."""

    log = _HookLog()
    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        log.events.append(f"http:{http_calls}")
        batch = json.loads(request.content)["input"]
        if http_calls == 2:
            return httpx.Response(429)
        return _embedding_response([(index, [1.0, 0.0, 0.0]) for index in range(len(batch))])

    client = _client(handler)
    try:
        vectors = await client.embed(
            _embedding_material(),
            ["a", "b", "c"],
            batch_guard=log.guard,
            on_batch_verified=log.verified,
        )
    finally:
        await client.aclose()

    assert len(vectors) == 3
    assert log.events == [
        "guard:1",
        "http:1",
        "verified:2",
        "guard:2",
        "http:2",
        "guard:3",
        "http:3",
        "verified:1",
    ]


@pytest.mark.asyncio
async def test_embed_guard_failure_stops_undispatched_batches() -> None:
    """A guard failure between batches must propagate and keep the second
    provider request from ever being dispatched."""

    log = _HookLog()
    log.fail_at_guard_run = 2
    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        batch = json.loads(request.content)["input"]
        return _embedding_response([(index, [1.0, 0.0, 0.0]) for index in range(len(batch))])

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.embed(
                _embedding_material(),
                ["a", "b", "c"],
                batch_guard=log.guard,
                on_batch_verified=log.verified,
            )
    finally:
        await client.aclose()

    assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
    assert http_calls == 1
    assert log.events == ["guard:1", "verified:2", "guard:2"]


@pytest.mark.asyncio
async def test_embed_guard_failure_before_the_retry_prevents_the_second_attempt() -> None:
    """After a 429 the guard runs again before the in-client retry; its
    failure must stop the retry, so exactly one HTTP request is sent."""

    log = _HookLog()
    log.fail_at_guard_run = 2
    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(429)

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.embed(
                _embedding_material(),
                ["only"],
                batch_guard=log.guard,
                on_batch_verified=log.verified,
            )
    finally:
        await client.aclose()

    assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
    assert http_calls == 1
    assert log.events == ["guard:1", "guard:2"]


@pytest.mark.asyncio
async def test_retry_backs_off_and_honors_retry_after() -> None:
    """The single in-client retry waits before re-dispatching: the Provider's
    ``Retry-After`` (capped) when present, otherwise a jittered base delay."""

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if attempts == 3:
            raise httpx.ConnectError("drop")
        return _embedding_response([(0, [1.0, 0.0, 0.0])])

    client = KnowledgeModelClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=fake_sleep)
    try:
        await client.embed(_embedding_material(), ["a"])
        await client.embed(_embedding_material(), ["b"])
    finally:
        await client.aclose()

    assert attempts == 4
    assert delays[0] == 2.0  # Retry-After wins
    assert 0.5 <= delays[1] <= 1.5  # jittered default base delay
    # A hostile Retry-After never pins the caller for minutes.
    huge = 0

    def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal huge
        huge += 1
        return httpx.Response(503, headers={"Retry-After": "3600"}) if huge == 1 else _embedding_response([(0, [1.0, 0.0, 0.0])])

    delays.clear()
    client = KnowledgeModelClient(http=httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)), sleep=fake_sleep)
    try:
        await client.embed(_embedding_material(), ["c"])
    finally:
        await client.aclose()
    assert delays == [30.0]


@pytest.mark.asyncio
async def test_embed_applies_the_material_prefix_for_the_requested_side() -> None:
    """Asymmetric models: queries and passages carry their own prefixes on the
    wire while the caller's texts stay untouched; no prefix means no change."""

    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.record(request)
        batch = json.loads(request.content)["input"]
        return _embedding_response([(index, [1.0, 0.0, 0.0]) for index in range(len(batch))])

    material = _embedding_material(query_prefix="query: ", passage_prefix="passage: ")
    client = _client(handler)
    try:
        await client.embed(material, ["问题"], kind="query")
        await client.embed(material, ["正文"], kind="passage")
        await client.embed(material, ["默认"])
        await client.embed(_embedding_material(), ["无前缀"], kind="query")
    finally:
        await client.aclose()

    inputs = [body["input"] for _, body in recorder.requests]
    assert inputs == [["query: 问题"], ["passage: 正文"], ["passage: 默认"], ["无前缀"]]


@pytest.mark.asyncio
async def test_embed_dispatches_batches_concurrently_and_keeps_input_order() -> None:
    """With ``embed_concurrency`` above one, batches overlap up to that bound,
    vectors still come back in input order, and the verified hook is
    serialized so progress stays monotonic."""

    import asyncio

    in_flight = 0
    peak = 0
    verified: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        batch = json.loads(request.content)["input"]
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _embedding_response([(0, [float(batch[0][-1]) + 1.0, 0.0, 0.0])])

    async def on_verified(count: int) -> None:
        verified.append(count)

    client = KnowledgeModelClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), embed_concurrency=3)
    try:
        vectors = await client.embed(_embedding_material(max_batch=1), [f"t{index}" for index in range(6)], on_batch_verified=on_verified)
    finally:
        await client.aclose()

    assert vectors == [[float(index) + 1.0, 0.0, 0.0] for index in range(6)]
    assert peak == 3
    assert verified == [1] * 6


@pytest.mark.asyncio
async def test_rerank_runs_the_batch_guard_and_stops_undispatched_batches() -> None:
    """The reranker path takes the same guard: batch one runs guarded, and a
    guard failure before batch two keeps that request undispatched."""

    log = _HookLog()
    log.fail_at_guard_run = 2
    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        body = json.loads(request.content)
        return _rerank_response([(index, 0.9 - index * 0.1) for index in range(body["top_n"])])

    client = _client(handler)
    try:
        with pytest.raises(KnowledgeError) as error:
            await client.rerank(
                _rerank_material(),
                "query",
                ["doc a", "doc b", "doc c"],
                top_n=3,
                batch_guard=log.guard,
            )
    finally:
        await client.aclose()

    assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
    assert http_calls == 1
    assert log.events == ["guard:1", "guard:2"]
