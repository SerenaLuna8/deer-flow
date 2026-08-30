"""SiliconFlow-compatible Embedding and Reranker HTTP client.

The client speaks exactly two contracts — ``POST {base_url}/embeddings`` and
``POST {base_url}/rerank`` — and validates every response before anything
downstream may store or rank with it. There is no provider plugin registry.

Error mapping follows the model-access design document: transport failures and
timeouts become ``KNOWLEDGE_MODEL_UNAVAILABLE``; provider HTTP errors and
malformed payloads become ``KNOWLEDGE_EMBEDDING_FAILED`` or
``KNOWLEDGE_RERANK_FAILED`` for their respective endpoint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

import httpx

from ..contracts import (
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_RERANK_FAILED,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeRerankMaterial,
)

EMBEDDING_PROBE_TEXT = "ActWeave knowledge embedding connection test"
RERANK_PROBE_QUERY = "ActWeave knowledge rerank connection test"
RERANK_PROBE_DOCUMENTS = (
    "ActWeave knowledge rerank connection test candidate one",
    "ActWeave knowledge rerank connection test candidate two",
)

# Connection probes run inside admin requests (and while the update flow holds
# a row lock), so they never use the full production timeout of up to 300s.
PROBE_TIMEOUT_SECONDS_CAP = 30


@dataclass(frozen=True, slots=True)
class RerankScore:
    """Reranker relevance for the candidate at ``index`` in the original list."""

    index: int
    score: float


def _is_finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


class KnowledgeModelClient:
    """Batched, validated access to SiliconFlow-compatible endpoints."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        # trust_env=False: ambient HTTP(S)_PROXY must never reroute requests
        # carrying the Bearer API key. follow_redirects=False is the httpx
        # default, made explicit: a 3xx can never re-send the key elsewhere.
        self._http = http or httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def embed(self, material: KnowledgeEmbeddingMaterial, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` in input order, batching by ``max_batch``."""

        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), material.max_batch):
            batch = texts[start : start + material.max_batch]
            payload = await self._post_with_retry(
                base_url=material.base_url,
                api_key=material.api_key,
                request_timeout_seconds=material.request_timeout_seconds,
                path="/embeddings",
                body={
                    "model": material.model_name,
                    "input": batch,
                    "dimensions": material.dimension,
                    "encoding_format": "float",
                },
                failure_code=KNOWLEDGE_EMBEDDING_FAILED,
            )
            vectors.extend(
                _validated_embedding_batch(
                    payload,
                    batch_size=len(batch),
                    dimension=material.dimension,
                )
            )
        return vectors

    async def rerank(
        self,
        material: KnowledgeRerankMaterial,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankScore]:
        """Score ``documents`` against ``query`` and return the best ``top_n``.

        Candidates are batched by ``max_batch`` with per-batch
        ``top_n = min(top_n, batch size)``; batch-local indexes are mapped back
        through the batch offset and batches merge by ``relevance_score``.
        """

        if not documents or top_n < 1:
            return []
        merged: list[RerankScore] = []
        for start in range(0, len(documents), material.max_batch):
            batch = documents[start : start + material.max_batch]
            batch_top_n = min(top_n, len(batch))
            payload = await self._post_with_retry(
                base_url=material.base_url,
                api_key=material.api_key,
                request_timeout_seconds=material.request_timeout_seconds,
                path="/rerank",
                body={
                    "model": material.model_name,
                    "query": query,
                    "documents": batch,
                    "top_n": batch_top_n,
                    "return_documents": False,
                },
                failure_code=KNOWLEDGE_RERANK_FAILED,
            )
            merged.extend(
                _validated_rerank_batch(
                    payload,
                    batch_size=len(batch),
                    top_n=batch_top_n,
                    offset=start,
                )
            )
        # Stable sort: equal scores keep the original candidate order, so the
        # cross-batch merge stays deterministic without re-scoring.
        merged.sort(key=lambda item: item.score, reverse=True)
        return merged[:top_n]

    async def verify_embedding(self, material: KnowledgeEmbeddingMaterial) -> None:
        """Probe ``/embeddings`` with a fixed text; raise on failure.

        The probe never creates business data, and its timeout is clamped to
        :data:`PROBE_TIMEOUT_SECONDS_CAP` so a black-holed provider cannot pin
        an admin request (or the registry update flow) for the full
        production timeout.
        """

        probe_material = replace(
            material,
            request_timeout_seconds=min(material.request_timeout_seconds, PROBE_TIMEOUT_SECONDS_CAP),
        )
        await self.embed(probe_material, [EMBEDDING_PROBE_TEXT])

    async def verify_rerank(self, material: KnowledgeRerankMaterial) -> None:
        """Probe ``/rerank`` with fixed candidates; raise on failure.

        A provider that answers the probe without any result is treated as a
        rerank failure. The timeout clamp matches :meth:`verify_embedding`.
        """

        probe_material = replace(
            material,
            request_timeout_seconds=min(material.request_timeout_seconds, PROBE_TIMEOUT_SECONDS_CAP),
        )
        scores = await self.rerank(
            probe_material,
            RERANK_PROBE_QUERY,
            list(RERANK_PROBE_DOCUMENTS),
            top_n=len(RERANK_PROBE_DOCUMENTS),
        )
        if not scores:
            raise KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 连接测试未返回任何结果")

    async def _post_with_retry(
        self,
        *,
        base_url: str,
        api_key: str,
        request_timeout_seconds: int,
        path: str,
        body: dict[str, Any],
        failure_code: str,
    ) -> Any:
        """POST once, retrying a single time on transport errors, 429 and 5xx."""

        url = base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = httpx.Timeout(request_timeout_seconds)
        for attempt in (1, 2):
            try:
                response = await self._http.post(url, json=body, headers=headers, timeout=timeout)
            except httpx.HTTPError:
                if attempt == 1:
                    continue
                raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "无法连接模型服务或请求超时") from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 1:
                    continue
                raise KnowledgeError(failure_code, f"模型服务返回 HTTP {response.status_code}")
            if response.status_code >= 400:
                raise KnowledgeError(failure_code, f"模型服务返回 HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise KnowledgeError(failure_code, "模型服务返回了无法解析的响应") from None
        raise KnowledgeError(failure_code, "模型服务请求未完成")


def _validated_embedding_batch(
    payload: Any,
    *,
    batch_size: int,
    dimension: int,
) -> list[list[float]]:
    """Validate one ``/embeddings`` response and restore input order.

    Providers honoring the OpenAI contract number ``data`` globally, so any
    permutation of ``0..n-1`` is re-ordered by ``index``. SiliconFlow shards
    large batches internally (observed shard size 8) and resets ``index`` to
    zero at every shard boundary while keeping ``data`` itself in input
    order; that reset pattern is accepted with array order. Any other index
    sequence cannot be mapped back to the input and is rejected.
    """

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 响应缺少 data 数组")
    data = payload["data"]
    if len(data) != batch_size:
        raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 响应数量与输入数量不一致")
    indexes: list[int] = []
    vectors: list[list[float]] = []
    for item in data:
        if not isinstance(item, dict):
            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 响应条目格式错误")
        index = item.get("index")
        if type(index) is not int or not 0 <= index < batch_size:
            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 响应 index 无法恢复输入顺序")
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or len(embedding) != dimension:
            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 向量维度与配置不一致")
        if not all(_is_finite_number(value) for value in embedding):
            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 向量包含非法数值")
        if not any(value != 0 for value in embedding):
            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 返回了全零向量")
        indexes.append(index)
        vectors.append([float(value) for value in embedding])
    if sorted(indexes) == list(range(batch_size)):
        order = sorted(range(batch_size), key=lambda position: indexes[position])
        return [vectors[position] for position in order]
    previous = -1
    for index in indexes:
        # Concatenated 0-based shards: each index either continues its shard
        # or starts a new one at zero (the first item must start at zero).
        if index not in (0, previous + 1):
            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 响应 index 无法恢复输入顺序")
        previous = index
    return vectors


def _validated_rerank_batch(
    payload: Any,
    *,
    batch_size: int,
    top_n: int,
    offset: int,
) -> list[RerankScore]:
    """Validate one ``/rerank`` response and map indexes back through ``offset``.

    Relevance scores must be finite and within ``[0, 1]``; out-of-range values
    are rejected rather than clamped, because downstream thresholds and the
    query log treat rerank scores as bounded relevance. The result count must
    equal the requested ``top_n`` exactly: retrieval asks for every candidate
    to be scored, so a provider that truncates or filters internally would
    otherwise drop candidates before thresholds and the global ranking run.
    """

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 响应缺少 results 数组")
    results = payload["results"]
    if len(results) != top_n:
        raise KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 响应结果数量与请求的 top_n 不一致")
    seen: set[int] = set()
    scores: list[RerankScore] = []
    previous_score: float | None = None
    for item in results:
        if not isinstance(item, dict):
            raise KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 响应条目格式错误")
        index = item.get("index")
        if type(index) is not int or not 0 <= index < batch_size or index in seen:
            raise KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 响应 index 重复或越界")
        score = item.get("relevance_score")
        if not _is_finite_number(score) or not 0 <= score <= 1:
            raise KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 分数包含非法数值或超出 [0,1] 范围")
        score_value = float(score)
        if previous_score is not None and score_value > previous_score:
            raise KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 响应未按分数降序返回")
        previous_score = score_value
        seen.add(index)
        scores.append(RerankScore(index=offset + index, score=score_value))
    return scores
