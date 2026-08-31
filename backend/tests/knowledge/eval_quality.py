"""M10 T14 real-model evaluation runner.

Uses an isolated Schema V1 database and the production search service plus
``KnowledgeModelClient``. Candidate identities come from frozen
``source_id + position + content digest`` stored in ``source_position``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from actweave_knowledge import (
    KnowledgeError,
    KnowledgeMetadataFilter,
    KnowledgeSearchRequest,
)
from actweave_knowledge.models.client import KnowledgeModelClient
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeMetadataFieldRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
)
from actweave_knowledge.retrieval import lexical_index_input
from actweave_knowledge.retrieval.service import KnowledgeSearchService
from eval_metrics import (
    NDCG_K,
    false_recall_rate,
    mean_or_none,
    ndcg_at_k,
    recall_hit,
)
from registry_helpers import (
    registry_model_port,
    registry_secret_key,
    seed_embedding_model,
    seed_provider,
    seed_rerank_model,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.model_registry.secrets import protect_provider_api_key
from app.system_settings.models import CreateSystemModel
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.model_registry import ModelProviderRow

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m10_retrieval_cases.json"
REPORT_JSON_PATH = Path(__file__).parents[3] / "docs" / "knowledge" / "m10-quality-eval-report.json"
REPORT_MD_PATH = Path(__file__).parents[3] / "docs" / "knowledge" / "m10-quality-eval-report.md"
ROOT_ENV_PATH = Path(__file__).parents[3] / ".env"

PRIMARY_EMBEDDING = "Qwen/Qwen3-Embedding-8B"
SECONDARY_EMBEDDING = "Qwen/Qwen3-Embedding-0.6B"
RERANKER = "Qwen/Qwen3-Reranker-8B"
EMBED_DIMENSION = 1024
PROVIDER_BASE_URL = "https://api.siliconflow.cn/v1"
MAX_BATCH = 32
REQUEST_TIMEOUT = 120

# Legacy unverified rates retained for old estimate comparability only.
# They are not current prices or measured billing; this file never stores a key.
PRICE_CNY_PER_MILLION = {
    PRIMARY_EMBEDDING: 0.28,
    SECONDARY_EMBEDDING: 0.07,
    RERANKER: 0.28,
}


def load_corpus() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def load_legacy_m10_corpus() -> dict[str, Any]:
    """Keep a newly authorized M10-equivalent run on the original frozen scope."""
    corpus = load_corpus()
    corpus["queries"] = [query for query in corpus["queries"] if query["category"] != "question_style"]
    corpus["documents"] = [document for document in corpus["documents"] if not document["source_id"].startswith("m11-question-")]
    frozen = {
        "queries": (65, "fb20703fa950571050300e172ec0457f2250b04340e3f1356401652fda4394e5"),
        "documents": (20, "f27ca1a442f7d4a2bd8726915ebff54f7e1f560840e133ad57ad8301711ea52b"),
    }
    for kind, (count, digest) in frozen.items():
        canonical = json.dumps(corpus[kind], ensure_ascii=False, sort_keys=True)
        if len(corpus[kind]) != count or content_digest(canonical) != digest:
            raise ValueError("Fresh M10-equivalent evaluation requires the original frozen corpus")
    return corpus


def read_env_file_value(name: str) -> str | None:
    if not ROOT_ENV_PATH.is_file():
        return None
    prefix = f"{name}="
    for raw in ROOT_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(prefix) and not line.startswith("#"):
            value = line.split("=", 1)[1].strip().strip("'").strip('"')
            return value or None
    return None


def resolve_provider_api_key() -> str | None:
    for name in (
        "ACT_WEAVE_KNOWLEDGE_QUALITY_API_KEY",
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY",
        "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY",
    ):
        value = os.environ.get(name, "").strip() or read_env_file_value(name)
        if value:
            return value
    return None


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def identity_key(source_id: str, position: int, content: str) -> str:
    return f"{source_id}:{position}:{content_digest(content)}"


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


class CountingClient:
    """Delegates to the real client and records call/token estimates."""

    def __init__(self, inner: KnowledgeModelClient) -> None:
        self._inner = inner
        self.embed_calls = 0
        self.embed_texts = 0
        self.embed_tokens = 0
        self.embed_tokens_by_model: dict[str, int] = defaultdict(int)
        self.rerank_calls = 0
        self.rerank_docs = 0
        self.rerank_tokens = 0

    async def embed(self, material, texts, **kwargs):  # noqa: ANN001
        self.embed_calls += 1
        self.embed_texts += len(texts)
        self.embed_tokens += sum(_estimate_tokens(item) for item in texts)
        self.embed_tokens_by_model[material.model_name] += sum(_estimate_tokens(item) for item in texts)
        return await self._inner.embed(material, texts, **kwargs)

    async def rerank(self, material, query, documents, top_n, **kwargs):  # noqa: ANN001
        self.rerank_calls += 1
        self.rerank_docs += len(documents)
        self.rerank_tokens += _estimate_tokens(query) + sum(_estimate_tokens(item) for item in documents)
        return await self._inner.rerank(material, query, documents, top_n, **kwargs)


class EvalSearchService(KnowledgeSearchService):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.captured: list[Any] = []

    async def _recalled_candidates(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        candidates, semantic, lexical, summary = await super()._recalled_candidates(**kwargs)
        self.captured.extend(candidates)
        return candidates, semantic, lexical, summary

    def take_captured(self) -> list[Any]:
        items = self.captured
        self.captured = []
        return items


@dataclass
class EvalContext:
    factory: async_sessionmaker[AsyncSession]
    service: EvalSearchService
    client: CountingClient
    project_id: uuid.UUID
    owner_user_id: uuid.UUID
    bases: dict[str, uuid.UUID]
    corpus: dict[str, Any]


@dataclass
class QueryOutcome:
    query_id: str
    split: str
    category: str
    mode: str
    candidate_ids: list[str] = field(default_factory=list)
    hit_ids: list[str] = field(default_factory=list)
    recall_candidate: bool | None = None
    recall_at_10: bool | None = None
    ndcg_at_10: float | None = None
    returned: int = 0
    candidate_count: int = 0
    non_provider_ms: float = 0.0
    error: str | None = None
    heterogeneous_without_lexical_evidence: bool = False
    summary_enabled: bool | None = None
    query_embedding_cache_hits: int = 0
    query_embedding_cache_misses: int = 0


async def _seed_project(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
        ),
        {"user_id": str(user_id), "email": "m10-eval@example.invalid", "username": "m10_eval"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {
            "project_id": project_id,
            "slug": "m10-eval",
            "display_name": "M10 quality eval",
            "user_id": str(user_id),
        },
    )
    return project_id, user_id


async def _embed_unique(client: CountingClient, material, texts: list[str]) -> dict[str, list[float]]:  # noqa: ANN001
    unique: list[str] = []
    seen: set[str] = set()
    for item in texts:
        digest = content_digest(item)
        if digest not in seen:
            seen.add(digest)
            unique.append(item)
    vectors = await client.embed(material, unique) if unique else []
    return {content_digest(text): vector for text, vector in zip(unique, vectors, strict=True)}


async def build_eval_context(postgres_database_url: str, api_key: str, *, corpus: dict[str, Any] | None = None) -> EvalContext:
    engine = create_async_engine(postgres_database_url)
    inner = KnowledgeModelClient()
    try:
        return await _populate_eval_context(engine, CountingClient(inner), api_key, corpus=corpus)
    except BaseException:
        await inner.aclose()
        await engine.dispose()
        raise


async def _populate_eval_context(engine: Any, client: CountingClient, api_key: str, *, corpus: dict[str, Any] | None = None) -> EvalContext:
    corpus = corpus if corpus is not None else load_corpus()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)

    provider_id = await seed_provider(
        factory,
        base_url=PROVIDER_BASE_URL,
        request_timeout_seconds=REQUEST_TIMEOUT,
        api_key=api_key,
    )
    async with factory() as session, session.begin():
        provider = await session.get(ModelProviderRow, provider_id)
        assert provider is not None
        envelope = protect_provider_api_key(
            provider_id=provider_id,
            base_url=PROVIDER_BASE_URL,
            api_key=api_key,
            key=registry_secret_key(),
        )
        provider.api_key_nonce = envelope.nonce
        provider.api_key_ciphertext = envelope.ciphertext
        provider.base_url = PROVIDER_BASE_URL
        provider.request_timeout_seconds = REQUEST_TIMEOUT

    primary_id = await seed_embedding_model(
        factory,
        provider_id,
        dimension=EMBED_DIMENSION,
        max_batch=MAX_BATCH,
        model_name=PRIMARY_EMBEDDING,
    )
    secondary_id = await seed_embedding_model(
        factory,
        provider_id,
        dimension=EMBED_DIMENSION,
        max_batch=MAX_BATCH,
        model_name=SECONDARY_EMBEDDING,
    )
    rerank_id = await seed_rerank_model(
        factory,
        provider_id,
        max_batch=MAX_BATCH,
        model_name=RERANKER,
    )

    service = EvalSearchService(
        session_factory=factory,
        client=client,  # type: ignore[arg-type]
        model_port=registry_model_port(),
    )

    async with factory() as session, session.begin():
        project_id, owner_user_id = await _seed_project(session)

    port = registry_model_port()
    async with factory() as session, session.begin():
        primary_material = await port.embedding_material(session, primary_id)
        secondary_material = await port.embedding_material(session, secondary_id)

    primary_texts: list[str] = []
    secondary_texts: list[str] = []
    for document in corpus["documents"]:
        bucket = secondary_texts if document["base"] == "hetero_semantic" else primary_texts
        for segment in document["segments"]:
            if document["chunking_mode"] == "parent_child":
                bucket.extend(child["content"] for child in segment["children"])
            else:
                bucket.append(segment["content"])
    filler_prototype = "华北冬小麦评测填充段，与运维标识符和产品手册无关。"
    primary_texts.append(filler_prototype)

    primary_vectors = await _embed_unique(client, primary_material, primary_texts)
    secondary_vectors = await _embed_unique(client, secondary_material, secondary_texts)
    filler_vector = primary_vectors[content_digest(filler_prototype)]

    bases: dict[str, uuid.UUID] = {}
    async with factory() as session, session.begin():
        specs = (
            ("large_hybrid", "评测大库", primary_id, rerank_id, "hybrid"),
            ("small_hybrid", "评测小库", primary_id, rerank_id, "hybrid"),
            ("parent_child_hybrid", "评测父子库", primary_id, rerank_id, "hybrid"),
            ("hetero_semantic", "评测异构库", secondary_id, rerank_id, "semantic"),
        )
        for key, name, embedding_id, bound_rerank, mode in specs:
            row = KnowledgeBaseRow(
                id=uuid.uuid4(),
                project_id=project_id,
                name=name,
                embedding_model_id=embedding_id,
                reranker_model_id=bound_rerank,
                retrieval_mode=mode,
                default_top_k=10,
                default_score_threshold=0.2,
            )
            session.add(row)
            bases[key] = row.id
        await session.flush()
        session.add(
            KnowledgeMetadataFieldRow(
                id=uuid.uuid4(),
                project_id=project_id,
                knowledge_base_id=bases["large_hybrid"],
                name="dept",
                field_type="string",
            )
        )

        annotated_units = 0
        for document in corpus["documents"]:
            base_id = bases[document["base"]]
            document_id = uuid.uuid4()
            row = KnowledgeDocumentRow(
                id=document_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                name=document["source_id"],
                original_name=f"{document['source_id']}.md",
                storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.md",
                size_bytes=128,
                status="ready",
                version=1,
                published_version=1,
                chunking_mode=document["chunking_mode"],
                doc_metadata=document.get("metadata") or {},
            )
            session.add(row)
            await session.flush()
            for segment in document["segments"]:
                content = segment["content"]
                source_position = {"source_id": document["source_id"], "position": segment["position"]}
                parent = KnowledgeSegmentRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    document_version=1,
                    position=segment["position"],
                    content=content,
                    word_count=len(content),
                    source_position=source_position,
                    embedding=None if document["chunking_mode"] == "parent_child" else primary_vectors[content_digest(content)] if document["base"] != "hetero_semantic" else secondary_vectors[content_digest(content)],
                    lexical_tsv=func.to_tsvector("simple", lexical_index_input(content)),
                    lexical_version=1,
                )
                session.add(parent)
                await session.flush()
                if document["chunking_mode"] == "parent_child":
                    for child in segment["children"]:
                        session.add(
                            KnowledgeSegmentChildRow(
                                id=uuid.uuid4(),
                                project_id=project_id,
                                knowledge_base_id=base_id,
                                knowledge_document_id=document_id,
                                knowledge_segment_id=parent.id,
                                document_version=1,
                                position=child["position"],
                                content=child["content"],
                                word_count=len(child["content"]),
                                embedding=primary_vectors[content_digest(child["content"])],
                                lexical_tsv=func.to_tsvector("simple", lexical_index_input(child["content"])),
                                lexical_version=1,
                            )
                        )
                        annotated_units += 1
                else:
                    annotated_units += 1

        filler_total = max(0, int(corpus["parameters"]["scale_retrieval_units"]) - annotated_units)
        filler_doc_id = uuid.uuid4()
        session.add(
            KnowledgeDocumentRow(
                id=filler_doc_id,
                project_id=project_id,
                knowledge_base_id=bases["large_hybrid"],
                name="scale-fillers",
                original_name="scale-fillers.md",
                storage_key=f"projects/{project_id}/knowledge/{bases['large_hybrid']}/{filler_doc_id}.md",
                size_bytes=filler_total,
                status="ready",
                version=1,
                published_version=1,
            )
        )
        await session.flush()
        batch: list[KnowledgeSegmentRow] = []
        for index in range(1, filler_total + 1):
            content = f"{filler_prototype} 序号 {index}。本旬日照 {index % 13} 小时。"
            batch.append(
                KnowledgeSegmentRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=bases["large_hybrid"],
                    knowledge_document_id=filler_doc_id,
                    document_version=1,
                    position=index,
                    content=content,
                    word_count=len(content),
                    source_position={"source_id": "scale-fillers", "position": index},
                    embedding=filler_vector,
                    lexical_tsv=func.to_tsvector("simple", lexical_index_input(content)),
                    lexical_version=1,
                )
            )
            if len(batch) >= 250:
                session.add_all(batch)
                await session.flush()
                batch.clear()
        if batch:
            session.add_all(batch)
            await session.flush()

    return EvalContext(
        factory=factory,
        service=service,
        client=client,
        project_id=project_id,
        owner_user_id=owner_user_id,
        bases=bases,
        corpus=corpus,
    )


def _scope_ids(ctx: EvalContext, scope: str) -> tuple[uuid.UUID, ...]:
    if scope == "parent_child":
        return (ctx.bases["parent_child_hybrid"],)
    if scope == "hetero":
        return (ctx.bases["large_hybrid"], ctx.bases["hetero_semantic"])
    return (ctx.bases["large_hybrid"], ctx.bases["small_hybrid"])


def judgment_identities(query: dict[str, Any]) -> dict[str, int]:
    return {f"{item['source_id']}:{item['position']}:{item['content_sha256']}": item["grade"] for item in query["judgments"]}


def _candidate_identity(candidate: Any) -> str:
    source = candidate.source_position or {}
    return f"{source.get('source_id')}:{source.get('position')}:{content_digest(candidate.content)}"


async def run_query(ctx: EvalContext, query: dict[str, Any], *, mode: str) -> QueryOutcome:
    filters = None
    raw_filters = query.get("metadata_filters")
    if raw_filters:
        filters = tuple(
            KnowledgeMetadataFilter(
                name=item["name"],
                operator=item["operator"],
                value=item["value"],
                field_kind=item.get("field_kind", "custom"),
            )
            for item in raw_filters
        )
    request = KnowledgeSearchRequest(
        project_id=ctx.project_id,
        owner_user_id=ctx.owner_user_id,
        query=query["query"],
        knowledge_base_ids=_scope_ids(ctx, query.get("base_scope") or "same_domain"),
        top_k=int(ctx.corpus["parameters"]["top_k"]),
        score_threshold=float(ctx.corpus["parameters"]["score_threshold"]),
        retrieval_mode=mode,  # type: ignore[arg-type]
        metadata_filters=filters,
        debug=True,
    )
    outcome = QueryOutcome(
        query_id=query["id"],
        split=query["split"],
        category=query["category"],
        mode=mode,
    )
    try:
        result = await ctx.service.search(request)
    except KnowledgeError as error:
        ctx.service.take_captured()
        outcome.error = error.code
        return outcome

    captured = ctx.service.take_captured()
    outcome.candidate_ids = [_candidate_identity(item) for item in captured]
    outcome.hit_ids = [f"{(hit.citation.source_position or {}).get('source_id')}:{hit.citation.segment_position}:{content_digest(hit.passage)}" for hit in result.hits]
    outcome.returned = len(result.hits)
    outcome.candidate_count = len(outcome.candidate_ids)
    diagnostics = result.diagnostics
    if diagnostics is not None:
        outcome.non_provider_ms = diagnostics.timings.recall_ms + diagnostics.timings.final_validation_ms
        outcome.heterogeneous_without_lexical_evidence = diagnostics.heterogeneous_without_lexical_evidence
        outcome.query_embedding_cache_hits = diagnostics.counts.query_embedding_cache_hits
        outcome.query_embedding_cache_misses = diagnostics.counts.query_embedding_cache_misses

    judgments = judgment_identities(query)
    targets = [key for key, grade in judgments.items() if grade == 2]
    if query["category"] == "no_answer":
        return outcome
    outcome.recall_candidate = recall_hit(targets, outcome.candidate_ids)
    outcome.recall_at_10 = recall_hit(targets, outcome.hit_ids[:NDCG_K])
    outcome.ndcg_at_10 = ndcg_at_k(judgments, outcome.hit_ids, NDCG_K)
    return outcome


def summarize(outcomes: list[QueryOutcome]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[QueryOutcome]] = defaultdict(list)
    for item in outcomes:
        grouped[(item.split, item.category, item.mode)].append(item)

    summary: dict[str, Any] = {}
    for (split, category, mode), rows in grouped.items():
        node = summary.setdefault(split, {}).setdefault(category, {})
        if category == "no_answer":
            node[mode] = {
                "count": len(rows),
                "errors": sum(1 for row in rows if row.error),
                "false_recall": false_recall_rate([row.returned for row in rows if row.error is None]),
                "mean_returned": mean_or_none([float(row.returned) for row in rows if row.error is None]),
                "p95_non_provider_ms": _p95([row.non_provider_ms for row in rows if row.error is None]),
            }
            continue
        node[mode] = {
            "count": len(rows),
            "errors": sum(1 for row in rows if row.error),
            "recall_candidate": mean_or_none([1.0 if row.recall_candidate else 0.0 for row in rows if row.error is None]),
            "recall_at_10": mean_or_none([1.0 if row.recall_at_10 else 0.0 for row in rows if row.error is None]),
            "ndcg_at_10": mean_or_none([row.ndcg_at_10 for row in rows if row.error is None and row.ndcg_at_10 is not None]),
            "mean_candidates": mean_or_none([float(row.candidate_count) for row in rows if row.error is None]),
            "p95_non_provider_ms": _p95([row.non_provider_ms for row in rows if row.error is None]),
            "misses": [row.query_id for row in rows if row.error is None and row.recall_at_10 is False],
        }
    return summary


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=20)[18]


def evaluate_gates(summary: dict[str, Any], corpus: dict[str, Any], *, latency_review: str | None = None) -> dict[str, Any]:
    gates = corpus["gates"]
    holdout = summary.get("holdout", {})
    identifier = holdout.get("identifier", {})
    natural = holdout.get("natural_language", {})
    no_answer = holdout.get("no_answer", {})
    tail = holdout.get("tail", {})

    hybrid_id = identifier.get("hybrid") or {}
    semantic_id = identifier.get("semantic") or {}
    hybrid_nl = natural.get("hybrid") or {}
    semantic_nl = natural.get("semantic") or {}
    hybrid_na = no_answer.get("hybrid") or {}
    semantic_na = no_answer.get("semantic") or {}
    hybrid_tail = tail.get("hybrid") or {}

    checks: dict[str, Any] = {}
    checks["identifier_recall_candidate_hybrid"] = {
        "actual": hybrid_id.get("recall_candidate"),
        "min": gates["identifier_recall_candidate_min"],
        "passed": _gte(hybrid_id.get("recall_candidate"), gates["identifier_recall_candidate_min"]),
    }
    checks["identifier_recall_at_10_hybrid"] = {
        "actual": hybrid_id.get("recall_at_10"),
        "min": gates["identifier_recall_at_10_min"],
        "passed": _gte(hybrid_id.get("recall_at_10"), gates["identifier_recall_at_10_min"]),
    }
    checks["identifier_m9_semantic_recall_at_10"] = {
        "actual": semantic_id.get("recall_at_10"),
        "note": "M9-equivalent path is retrieval_mode=semantic on the same frozen models",
    }
    nl_recall_drop = _drop(semantic_nl.get("recall_at_10"), hybrid_nl.get("recall_at_10"))
    checks["natural_language_recall_regression"] = {
        "baseline": semantic_nl.get("recall_at_10"),
        "actual": hybrid_nl.get("recall_at_10"),
        "drop": nl_recall_drop,
        "max_drop": gates["natural_language_recall_regression_max"],
        "passed": nl_recall_drop is not None and nl_recall_drop <= gates["natural_language_recall_regression_max"],
    }
    nl_ndcg_drop = _drop(semantic_nl.get("ndcg_at_10"), hybrid_nl.get("ndcg_at_10"))
    checks["natural_language_ndcg_regression"] = {
        "baseline": semantic_nl.get("ndcg_at_10"),
        "actual": hybrid_nl.get("ndcg_at_10"),
        "drop": nl_ndcg_drop,
        "max_drop": gates["natural_language_ndcg_regression_max"],
        "passed": nl_ndcg_drop is not None and nl_ndcg_drop <= gates["natural_language_ndcg_regression_max"],
    }
    checks["no_answer_false_recall"] = {
        "baseline": semantic_na.get("false_recall"),
        "actual": hybrid_na.get("false_recall"),
        "passed": _lte(hybrid_na.get("false_recall"), semantic_na.get("false_recall")),
    }
    checks["tail_zero_miss"] = {
        "misses": hybrid_tail.get("misses") or [],
        "passed": hybrid_tail.get("recall_at_10") == 1.0,
    }
    p95_sem = semantic_nl.get("p95_non_provider_ms")
    p95_hyb = hybrid_nl.get("p95_non_provider_ms")
    ratio = p95_hyb / p95_sem if _finite_number(p95_sem) and p95_sem > 0 and _finite_number(p95_hyb) else None
    review_required = ratio is not None and ratio > gates["p95_regression_review_ratio"]
    review = latency_review.strip() if isinstance(latency_review, str) and latency_review.strip() else None
    review_recorded = review_required and review is not None
    checks["p95_non_provider"] = {
        "baseline_ms": p95_sem,
        "actual_ms": p95_hyb,
        "ratio": ratio,
        "review_ratio": gates["p95_regression_review_ratio"],
        "review_required": review_required,
        "review": review if review_required else None,
        "passed": ratio is not None and (not review_required or review_recorded),
    }
    quality_keys = (
        "identifier_recall_candidate_hybrid",
        "identifier_recall_at_10_hybrid",
        "natural_language_recall_regression",
        "natural_language_ndcg_regression",
        "no_answer_false_recall",
        "tail_zero_miss",
    )
    quality_passed = all(checks[key]["passed"] is True for key in quality_keys)
    return {
        "quality_passed": quality_passed,
        "all_passed": quality_passed and checks["p95_non_provider"]["passed"],
        "p95_review_recorded": review_recorded,
        "p95_review_pending": review_required and not review_recorded,
        "checks": checks,
    }


def _gte(actual: float | None, minimum: float) -> bool:
    return actual is not None and actual + 1e-12 >= minimum


def _lte(actual: float | None, baseline: float | None) -> bool:
    if actual is None or baseline is None:
        return False
    return actual <= baseline + 1e-12


def _drop(baseline: float | None, actual: float | None) -> float | None:
    if baseline is None or actual is None:
        return None
    return baseline - actual


def write_report(report: dict[str, Any]) -> None:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gates = report["gates"]["checks"]
    performance = gates.get("p95_non_provider", {})
    if performance.get("ratio") is None:
        review_text = "缺少有效的非 Provider P95 测量，不能判定性能门禁。"
    elif performance.get("review_required"):
        review_text = f"已记录操作者显式复审：{performance['review']}" if report["gates"].get("p95_review_recorded") else "待操作者显式复审：非 Provider P95 超过评测阈值，当前没有批准记录。测量本身不证明耗时增长的具体原因。"
    else:
        review_text = f"未触发复审：非 Provider P95 未超过基线的 {performance['review_ratio']} 倍。"
    lines = [
        "# M10 真实质量评测报告",
        "",
        f"- 运行时间：{report['ran_at']}",
        f"- 硬件：{report['hardware']}",
        f"- 模型：主嵌入 `{report['models']['primary_embedding']}` / 副嵌入 `{report['models']['secondary_embedding']}` / 重排 `{report['models']['reranker']}`",
        f"- 语料：{report['corpus_queries']} 题（holdout {report['holdout_queries']}，其中标识符 {report['holdout_identifiers']}），检索单元 {report['retrieval_units']}",
        "- 对照：semantic 视为 M9 等价路径（同模型、无词法）；hybrid 为 M10 路径。异构域另跑两模式。",
        (
            f"- 调用：embed {report['usage']['embed_texts']} 条 / 约 {report['usage']['embed_tokens']} token，"
            f"rerank {report['usage']['rerank_docs']} 篇 / 约 {report['usage']['rerank_tokens']} token，"
            f"旧费率估算 ¥{report['usage']['estimated_cny']:.4f}（费率未经当前验证，不是实测账单）"
        ),
        f"- 质量门槛：{'通过' if report['gates']['quality_passed'] else '未通过'}；总门禁：{'通过' if report['gates']['all_passed'] else '未通过'}。",
        f"- 性能评审：{review_text}",
        "",
        "## 验收集汇总",
        "",
        "```json",
        json.dumps(report["summary"].get("holdout", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## 门槛判定",
        "",
        "```json",
        json.dumps(report["gates"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 非 Provider P95 产品预算复审",
        "",
        review_text,
        "",
        "## 部署确认",
        "",
        report["deployment_note"],
        "",
    ]
    REPORT_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


async def count_retrieval_units(ctx: EvalContext) -> int:
    async with ctx.factory() as session:
        segments = await session.scalar(select(func.count()).select_from(KnowledgeSegmentRow))
        children = await session.scalar(select(func.count()).select_from(KnowledgeSegmentChildRow))
    return int(segments or 0) + int(children or 0)


async def run_quality_eval(postgres_database_url: str, *, api_key: str, latency_review: str | None = None) -> dict[str, Any]:
    ctx = await build_eval_context(postgres_database_url, api_key, corpus=load_legacy_m10_corpus())
    try:
        return await _run_legacy_quality_eval(ctx, latency_review=latency_review)
    finally:
        await ctx.client._inner.aclose()
        await ctx.factory.kw["bind"].dispose()


async def _run_legacy_quality_eval(ctx: EvalContext, *, latency_review: str | None = None) -> dict[str, Any]:
    outcomes: list[QueryOutcome] = []
    planned = len(ctx.corpus["queries"]) * 2
    for query in ctx.corpus["queries"]:
        for mode in ("semantic", "hybrid"):
            outcomes.append(await run_query(ctx, query, mode=mode))
            done = len(outcomes)
            if done == 1 or done % 10 == 0 or done == planned:
                last = outcomes[-1]
                print(
                    f"m10-eval {done}/{planned} {last.query_id}/{last.mode} recall10={last.recall_at_10} err={last.error}",
                    flush=True,
                )
    summary = summarize(outcomes)
    units = await count_retrieval_units(ctx)
    embed_cost = ctx.client.embed_tokens / 1_000_000 * PRICE_CNY_PER_MILLION[PRIMARY_EMBEDDING]
    rerank_cost = ctx.client.rerank_tokens / 1_000_000 * PRICE_CNY_PER_MILLION[RERANKER]
    report = {
        "baseline_kind": "fresh_m10_equivalent",
        "ran_at": datetime.now(UTC).isoformat(),
        "hardware": f"{platform.system()} {platform.release()} {platform.machine()} {platform.processor()}",
        "models": {
            "primary_embedding": PRIMARY_EMBEDDING,
            "secondary_embedding": SECONDARY_EMBEDDING,
            "reranker": RERANKER,
            "dimension": EMBED_DIMENSION,
            "m9_equivalent": "retrieval_mode=semantic",
            "m10": "retrieval_mode=hybrid",
        },
        "corpus_queries": len(ctx.corpus["queries"]),
        "holdout_queries": sum(1 for item in ctx.corpus["queries"] if item["split"] == "holdout"),
        "holdout_identifiers": sum(1 for item in ctx.corpus["queries"] if item["split"] == "holdout" and item["category"] == "identifier"),
        "retrieval_units": units,
        "usage": {
            "embed_calls": ctx.client.embed_calls,
            "embed_texts": ctx.client.embed_texts,
            "embed_tokens": ctx.client.embed_tokens,
            "rerank_calls": ctx.client.rerank_calls,
            "rerank_docs": ctx.client.rerank_docs,
            "rerank_tokens": ctx.client.rerank_tokens,
            "estimated_cny": round(embed_cost + rerank_cost, 4),
        },
        "summary": summary,
        "outcomes": [
            {
                "id": item.query_id,
                "split": item.split,
                "category": item.category,
                "mode": item.mode,
                "recall_candidate": item.recall_candidate,
                "recall_at_10": item.recall_at_10,
                "ndcg_at_10": item.ndcg_at_10,
                "returned": item.returned,
                "candidates": item.candidate_count,
                "non_provider_ms": item.non_provider_ms,
                "error": item.error,
                "heterogeneous_without_lexical_evidence": item.heterogeneous_without_lexical_evidence,
            }
            for item in outcomes
        ],
        "gates": evaluate_gates(summary, ctx.corpus, latency_review=latency_review),
        "deployment_note": ("本评测使用随机隔离空库，不是操作者目标库。M10 Schema 仍是一次定义、无升级路径；未取得目标库/停服/旧数据处置确认前，部署保持阻塞。"),
    }
    write_report(report)
    return report


# M11 is a separate evaluator so the frozen M10 release report and semantics
# remain reproducible. No real run is permitted without its own explicit budget.
M11_REPORT_JSON_PATH = REPORT_JSON_PATH.with_name("m11-quality-eval-report.json")
M11_REPORT_MD_PATH = REPORT_MD_PATH.with_name("m11-quality-eval-report.md")
M11_MODES = ("semantic", "hybrid")
M11_SUMMARY_AXES = ("off", "on")


def _validated_m11_summary_template(summary_model: str, *, base_url: str, template: CreateSystemModel | None = None) -> CreateSystemModel:
    """Validate an authoring DTO and its separate Provider binding without I/O."""
    from app.system_settings.validation import validate_create_system_model

    candidate = (
        template
        if template is not None
        else CreateSystemModel(
            display_name="M11 Summary Evaluation",
            status="active",
            provider_id=uuid.UUID(int=1),
            provider_adapter="openai",
            provider_model=summary_model,
            max_input_tokens=8192,
            settings={},
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=False,
        )
    )
    checked = validate_create_system_model(candidate)
    if checked.provider_model != summary_model:
        raise ValueError("M11 summary model must match its native configuration template")
    # The endpoint is Provider-owned, not an authored model setting. Validate
    # its effective combination before any source embedding, then let the
    # catalog inject it from the new isolated Provider during creation.
    validate_create_system_model(replace(checked, settings={**checked.settings, "base_url": base_url}), allow_derived_base_url=True)
    return checked


def m11_eval_preflight(corpus: dict[str, Any], *, summary_model: str | None, max_summary_calls: int | None, opted_in: bool, summary_model_template: CreateSystemModel | None = None, summary_base_url: str = PROVIDER_BASE_URL) -> int:
    """Validate authority and count eligible sources before source embedding I/O."""
    from actweave_knowledge import KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS

    if opted_in is not True:
        raise ValueError("M11 real-model evaluation requires explicit opt-in")
    if not isinstance(summary_model, str) or not summary_model.strip():
        raise ValueError("M11 requires an explicitly selected summary model")
    _validated_m11_summary_template(summary_model, base_url=summary_base_url, template=summary_model_template)
    eligible = sum(1 for document in corpus["documents"] for segment in document["segments"] if len(segment["content"]) >= KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS)
    if type(max_summary_calls) is not int or max_summary_calls < max(1, eligible):
        raise ValueError(f"M11 summary call budget must cover at least {max(1, eligible)} eligible segments")
    for split in ("dev", "holdout"):
        if sum(query["split"] == split and query["category"] == "question_style" for query in corpus["queries"]) < 10:
            raise ValueError("M11 requires at least ten question_style queries in both dev and holdout")
    return eligible


def summarize_m11(outcomes: list[QueryOutcome]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str, str], list[QueryOutcome]] = defaultdict(list)
    for row in outcomes:
        if type(row.summary_enabled) is not bool:
            continue  # absent axes cannot accidentally pass the coverage gate
        axis = "on" if row.summary_enabled else "off"
        grouped[row.split, row.category, row.mode, axis].append(row)
        grouped[row.split, "overall", row.mode, axis].append(row)
    summary: dict[str, Any] = {}
    for (split, category, mode, axis), rows in grouped.items():
        answered = [row for row in rows if row.category != "no_answer"]
        metrics = {
            "count": len(rows),
            "query_ids": sorted(row.query_id for row in rows),
            "errors": sum(row.error is not None for row in rows),
            "p95_non_provider_ms": _p95([row.non_provider_ms for row in rows if row.error is None]),
            "query_embedding_cache_hits": sum(row.query_embedding_cache_hits for row in rows),
            "query_embedding_cache_misses": sum(row.query_embedding_cache_misses for row in rows),
        }
        if category == "no_answer":
            # A failed query cannot be counted as an empty, correct answer.
            metrics["false_recall"] = mean_or_none([float(row.error is not None or row.returned > 0) for row in rows])
        else:
            metrics.update(
                {
                    "recall_candidate": mean_or_none([float(row.error is None and row.recall_candidate is True) for row in answered]),
                    "recall_at_10": mean_or_none([float(row.error is None and row.recall_at_10 is True) for row in answered]),
                    "ndcg_at_10": mean_or_none([row.ndcg_at_10 if row.error is None and row.ndcg_at_10 is not None else 0.0 for row in answered]),
                }
            )
        summary.setdefault(split, {}).setdefault(category, {}).setdefault(mode, {})[axis] = metrics
    return summary


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def resolve_m11_baseline_path(path: Path | None = None) -> Path:
    return (path or Path(os.environ.get("ACT_WEAVE_KNOWLEDGE_M11_BASELINE_REPORT") or REPORT_JSON_PATH)).expanduser()


def load_m11_baseline(corpus: dict[str, Any], path: Path) -> dict[str, Any]:
    """Reject missing or incomparable historical/fresh M10 evidence before I/O."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("M11 requires a readable verified M10 baseline report") from exc
    expected_models = {"primary_embedding": PRIMARY_EMBEDDING, "secondary_embedding": SECONDARY_EMBEDDING, "reranker": RERANKER}
    legacy_queries = [query for query in corpus["queries"] if query["category"] != "question_style"]
    if not isinstance(report, dict) or not isinstance(report.get("models"), dict) or report.get("corpus_queries") != len(legacy_queries) or any(report["models"].get(key) != value for key, value in expected_models.items()):
        raise ValueError("M11 baseline must use the original M10 query scope and matching models")
    expected_counts: dict[str, int] = defaultdict(int)
    for query in legacy_queries:
        if query["split"] == "holdout":
            expected_counts[query["category"]] += 1
    try:
        for category, count in expected_counts.items():
            for mode in M11_MODES:
                metrics = report["summary"]["holdout"][category][mode]
                rates = ("false_recall",) if category == "no_answer" else ("recall_candidate", "recall_at_10", "ndcg_at_10")
                valid_rates = all(_finite_number(metrics.get(name)) and 0 <= metrics[name] <= 1 for name in rates)
                p95 = metrics.get("p95_non_provider_ms")
                if metrics.get("count") != count or metrics.get("errors") != 0 or not valid_rates or not _finite_number(p95) or p95 <= 0:
                    raise ValueError("M11 baseline requires complete, successful M10 holdout measurements")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("M11 baseline is missing comparable M10 holdout evidence") from exc
    return report


def _m11_latency_check(before: Any, after: Any, *, limit: float, review: str | None) -> dict[str, Any]:
    ratio = after / before if _finite_number(before) and before > 0 and _finite_number(after) else None
    needs_review = ratio is not None and ratio > limit
    return {
        "baseline_ms": before,
        "actual_ms": after,
        "ratio": ratio,
        "review_ratio": limit,
        "review_required": needs_review,
        "operator_review": review if needs_review else None,
        "passed": ratio is not None and (not needs_review or review is not None),
    }


def evaluate_m11_gates(summary: dict[str, Any], corpus: dict[str, Any], *, baseline_report: dict[str, Any] | None = None, latency_review: str | None = None) -> dict[str, Any]:
    """Fail closed on missing/error evidence; performance approval is supplied, never invented."""
    limits = corpus["gates"]
    expected: dict[tuple[str, str], list[str]] = defaultdict(list)
    for query in corpus["queries"]:
        expected[query["split"], query["category"]].append(query["id"])
    coverage = bool(expected)
    errors = 0
    for (split, category), query_ids in expected.items():
        for mode in M11_MODES:
            for axis in M11_SUMMARY_AXES:
                node = summary.get(split, {}).get(category, {}).get(mode, {}).get(axis, {})
                coverage &= node.get("query_ids") == sorted(query_ids) and node.get("count") == len(query_ids)
                errors += int(node.get("errors", 0))
    coverage &= all(len(expected.get((split, "question_style"), [])) >= 10 for split in ("dev", "holdout"))
    checks: dict[str, Any] = {"complete_evidence": {"passed": bool(coverage), "errors": errors}, "query_errors": {"passed": errors == 0, "actual": errors}}
    holdout = summary.get("holdout", {})
    frozen = (baseline_report or {}).get("summary", {}).get("holdout", {})
    review = latency_review.strip() if isinstance(latency_review, str) and latency_review.strip() else None
    p95_review_pending = False
    for mode in M11_MODES:
        question = holdout.get("question_style", {}).get(mode, {})
        for metric, threshold_key in (("recall_candidate", "question_recall_candidate_uplift_pp"), ("recall_at_10", "question_recall_at_10_uplift_pp")):
            before, after = question.get("off", {}).get(metric), question.get("on", {}).get(metric)
            uplift = (after - before) * 100 if _finite_number(before) and _finite_number(after) else None
            checks[f"{mode}_question_{metric}"] = {"baseline": before, "actual": after, "uplift_pp": uplift, "minimum_pp": limits[threshold_key], "passed": uplift is not None and _gte(uplift, limits[threshold_key])}
        overall = holdout.get("overall", {}).get(mode, {})
        before, after = overall.get("off", {}).get("ndcg_at_10"), overall.get("on", {}).get("ndcg_at_10")
        drop = before - after if _finite_number(before) and _finite_number(after) else None
        checks[f"{mode}_overall_ndcg"] = {"drop": drop, "maximum_drop": limits["overall_ndcg_regression"], "passed": drop is not None and _lte(drop, limits["overall_ndcg_regression"])}
        no_answer = holdout.get("no_answer", {}).get(mode, {})
        before, after = no_answer.get("off", {}).get("false_recall"), no_answer.get("on", {}).get("false_recall")
        waterline = frozen.get("no_answer", {}).get(mode, {}).get("false_recall")
        valid = all(_finite_number(value) for value in (before, after, waterline))
        checks[f"{mode}_no_answer"] = {"baseline": before, "m10_waterline": waterline, "actual": after, "passed": limits["no_answer_false_recall_not_worse"] is True and valid and _lte(after, min(before, waterline))}
        for category in sorted({category for split, category in expected if split == "holdout"} - {"question_style", "no_answer"}):
            metrics = holdout.get(category, {}).get(mode, {})
            for metric in ("recall_candidate", "recall_at_10"):
                before, after = metrics.get("off", {}).get(metric), metrics.get("on", {}).get(metric)
                waterline = frozen.get(category, {}).get(mode, {}).get(metric)
                valid = all(_finite_number(value) for value in (before, after, waterline))
                checks[f"{mode}_{category}_{metric}"] = {"baseline": before, "m10_waterline": waterline, "actual": after, "passed": limits["existing_category_recall_not_worse"] is True and valid and _gte(after, max(before, waterline))}
        before, after = overall.get("off", {}).get("p95_non_provider_ms"), overall.get("on", {}).get("p95_non_provider_ms")
        paired = _m11_latency_check(before, after, limit=limits["p95_regression_review_ratio_m11"], review=review)
        checks[f"{mode}_p95"] = paired
        p95_review_pending |= paired["review_required"] and review is None
        # The original M10 report records category P95, not an overall P95.
        # Compare like-for-like categories so a slow OFF axis cannot hide a
        # regression against the separately verified M10 baseline.
        for category in sorted({category for split, category in expected if split == "holdout"} - {"question_style"}):
            before = frozen.get(category, {}).get(mode, {}).get("p95_non_provider_ms")
            after = holdout.get(category, {}).get(mode, {}).get("on", {}).get("p95_non_provider_ms")
            check = _m11_latency_check(before, after, limit=limits["p95_regression_review_ratio_m11"], review=review)
            checks[f"{mode}_{category}_m10_p95"] = check
            p95_review_pending |= check["review_required"] and review is None
    quality_passed = all(item["passed"] is True for key, item in checks.items() if not key.endswith("_p95"))
    return {"quality_passed": quality_passed, "all_passed": quality_passed and all(item["passed"] is True for item in checks.values()), "p95_review_pending": p95_review_pending, "checks": checks}


def _diagnostic_integer(value: Any, *, minimum: int = 0, maximum: int = 1_000_000_000) -> int | None:
    return value if type(value) is int and minimum <= value <= maximum else None


@dataclass
class SummaryDiagnostics:
    """Append-only observation scoped to this evaluator's single worker slot."""

    events: list[dict[str, Any]] = field(default_factory=list)
    task_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    task_attempt: int | None = None
    call_index: int | None = None

    def record(self, event: str, **safe_fields: Any) -> None:
        error_fields = {"exception_name", "exception_category", "http_status"}
        fields_by_event = {
            "runtime_error": error_fields | {"elapsed_ms"},
            "runtime_response": {"elapsed_ms", "content_kind", "content_length", "content_empty", "finish_reason", "token_usage"},
            "summary_call_error": error_fields,
            "task_attempt_error": error_fields,
            "budget_denied": {"admitted_calls", "call_budget"},
        }
        if type(event) is not str or event not in fields_by_event:
            return
        enum_values = {
            "exception_name": {
                "UnknownError",
                "CancelledError",
                "TimeoutError",
                "APITimeoutError",
                "RateLimitError",
                "AuthenticationError",
                "PermissionDeniedError",
                "BadRequestError",
                "APIConnectionError",
                "APIStatusError",
                "APIResponseValidationError",
                "LengthFinishReasonError",
                "ContentFilterFinishReasonError",
                "KnowledgeError",
                "ValueError",
            },
            "exception_category": {
                "unknown",
                "cancelled",
                "runtime_timeout",
                "provider_timeout",
                "rate_limit",
                "authentication",
                "permission",
                "bad_request",
                "connection",
                "http_error",
                "response_validation",
                "length_finish",
                "content_filter",
                "knowledge_error",
                "validation",
            },
            "content_kind": {"string", "list", "other"},
            "finish_reason": {"stop", "length", "content_filter", "tool_calls", "function_call", "unknown"},
        }
        filtered: dict[str, Any] = {}
        for key, value in safe_fields.items():
            if key not in fields_by_event[event]:
                continue
            if key in enum_values:
                if type(value) is str and value in enum_values[key]:
                    filtered[key] = value
            elif key == "elapsed_ms":
                if _finite_number(value) and 0 <= value <= 1_000_000_000:
                    filtered[key] = value
            elif key == "http_status":
                if value is None or _diagnostic_integer(value, minimum=100, maximum=599) is not None:
                    filtered[key] = value
            elif key == "content_empty":
                if value is None or type(value) is bool:
                    filtered[key] = value
            elif key == "token_usage":
                if type(value) is dict:
                    filtered[key] = {name: count for name, count in value.items() if name in {"input_tokens", "output_tokens", "total_tokens"} and _diagnostic_integer(count) is not None}
            elif _diagnostic_integer(value) is not None or (key == "content_length" and value is None):
                filtered[key] = value
        self.events.append(
            {
                "event_index": len(self.events) + 1,
                "event": event,
                "task_id": str(self.task_id) if type(self.task_id) is uuid.UUID else None,
                "document_id": str(self.document_id) if type(self.document_id) is uuid.UUID else None,
                "task_attempt": _diagnostic_integer(self.task_attempt),
                "call_index": _diagnostic_integer(self.call_index),
                **filtered,
            }
        )


def _safe_summary_exception(exc: BaseException) -> dict[str, Any]:
    import asyncio

    import openai

    allowed = (
        (asyncio.CancelledError, "cancelled"),
        (TimeoutError, "runtime_timeout"),
        (openai.APITimeoutError, "provider_timeout"),
        (openai.RateLimitError, "rate_limit"),
        (openai.AuthenticationError, "authentication"),
        (openai.PermissionDeniedError, "permission"),
        (openai.BadRequestError, "bad_request"),
        (openai.APIConnectionError, "connection"),
        (openai.APIStatusError, "http_error"),
        (openai.APIResponseValidationError, "response_validation"),
        (openai.LengthFinishReasonError, "length_finish"),
        (openai.ContentFilterFinishReasonError, "content_filter"),
        (KnowledgeError, "knowledge_error"),
        (ValueError, "validation"),
    )
    name, category = "UnknownError", "unknown"
    for error_type, candidate_category in allowed:
        if isinstance(exc, error_type):
            # Serialize only the trusted class label, never an unknown
            # subclass's name, message, body, request or response object.
            name, category = error_type.__name__, candidate_category
            break
    return {
        "exception_name": name,
        "exception_category": category,
        "http_status": _diagnostic_integer(exc.status_code, minimum=100, maximum=599) if isinstance(exc, openai.APIStatusError) else None,
    }


def _safe_summary_response(message: Any) -> dict[str, Any]:
    from langchain_core.messages import BaseMessage

    content = message.content if isinstance(message, BaseMessage) else None
    content_kind = "string" if type(content) is str else "list" if type(content) is list else "other"
    metadata = message.response_metadata if isinstance(message, BaseMessage) and type(message.response_metadata) is dict else {}
    reason = metadata.get("finish_reason")
    allowed_reasons = {"stop", "length", "content_filter", "tool_calls", "function_call"}
    usage = getattr(message, "usage_metadata", None) if isinstance(message, BaseMessage) else None
    if type(usage) is dict:
        token_values = {key: usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")}
    else:
        raw_usage = metadata.get("token_usage")
        raw_usage = raw_usage if type(raw_usage) is dict else {}
        token_values = {"input_tokens": raw_usage.get("prompt_tokens"), "output_tokens": raw_usage.get("completion_tokens"), "total_tokens": raw_usage.get("total_tokens")}
    return {
        "content_kind": content_kind,
        "content_length": len(content) if type(content) in (str, list) else None,
        "content_empty": not content.strip() if type(content) is str else not content if type(content) is list else None,
        "finish_reason": reason if type(reason) is str and reason in allowed_reasons else "unknown",
        "token_usage": {key: value for key, value in token_values.items() if _diagnostic_integer(value) is not None},
    }


class ObservedSummaryRuntime:
    """Observe before public error redaction; forward the exact runtime contract."""

    def __init__(self, delegate: Any, diagnostics: SummaryDiagnostics) -> None:
        self._delegate = delegate
        self._diagnostics = diagnostics

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            response = await self._delegate.ainvoke(*args, **kwargs)
        except BaseException as exc:
            self._diagnostics.record("runtime_error", elapsed_ms=round((time.monotonic() - started) * 1000, 3), **_safe_summary_exception(exc))
            raise
        self._diagnostics.record("runtime_response", elapsed_ms=round((time.monotonic() - started) * 1000, 3), **_safe_summary_response(response))
        return response


class BudgetedSummaryPort:
    """Hard provider-call budget, including failed attempts, for this evaluator."""

    def __init__(self, delegate: Any, *, max_calls: int, diagnostics: SummaryDiagnostics | None = None) -> None:
        self._delegate = delegate
        self.max_calls = max_calls
        self.calls = 0
        self.input_tokens_estimated = 0
        self.output_tokens_estimated = 0
        self.calls_by_document: dict[str, int] = defaultdict(int)
        self.diagnostics = diagnostics if diagnostics is not None else SummaryDiagnostics()
        # The evaluator uses one worker slot; this scope is set around each
        # actual durable task invocation, including retries.
        self.document_id: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def generate_summary(self, *, model_ref: str, prompt: str) -> str:
        from actweave_knowledge import KNOWLEDGE_TASK_FAILED

        if self.calls >= self.max_calls:
            self.diagnostics.record("budget_denied", admitted_calls=self.calls, call_budget=self.max_calls)
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "评测摘要调用预算已耗尽")
        self.calls += 1
        self.calls_by_document[self.document_id or "unattributed"] += 1
        self.input_tokens_estimated += _estimate_tokens(prompt)
        self.diagnostics.call_index = self.calls
        try:
            content = await self._delegate.generate_summary(model_ref=model_ref, prompt=prompt)
        except BaseException as exc:
            self.diagnostics.record("summary_call_error", **_safe_summary_exception(exc))
            raise
        finally:
            self.diagnostics.call_index = None
        self.output_tokens_estimated += _estimate_tokens(content)
        return content


def m11_cost_estimate(usage: dict[str, Any], approved_prices: dict[str, Any] | None) -> dict[str, Any] | None:
    """No inherited/public price guesses: only explicitly approved supplied rates."""
    if approved_prices is None:
        return None
    if not isinstance(approved_prices.get("approval_reference"), str) or not approved_prices["approval_reference"].strip() or not isinstance(approved_prices.get("currency"), str) or not approved_prices["currency"].strip():
        raise ValueError("cost estimation requires an explicit price approval reference and currency")
    token_counts = dict(usage.get("embed_tokens_by_model", {}))
    for model, amount in ((RERANKER, usage["rerank_tokens"]), ("summary_input", usage["summary_input_tokens_estimated"]), ("summary_output", usage["summary_output_tokens_estimated"])):
        token_counts[model] = token_counts.get(model, 0) + amount
    rates = approved_prices.get("per_million_tokens", {})
    if any(not _finite_number(rates.get(model)) or rates[model] < 0 for model in token_counts):
        raise ValueError("approved rates must cover every used model with a nonnegative finite price")
    return {
        "currency": approved_prices["currency"],
        "approval_reference": approved_prices["approval_reference"],
        "rates_per_million_tokens": dict(rates),
        "estimated_total": sum(tokens * rates[model] / 1_000_000 for model, tokens in token_counts.items()),
        "basis": "estimated text token counts; actual provider billing is not inferred",
    }


def blocked_m11_report(corpus: dict[str, Any]) -> dict[str, Any]:
    from actweave_knowledge import KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS

    reasons = ["final_summary_model_not_confirmed", "summary_call_budget_not_confirmed", "real_provider_run_not_authorized"]
    try:
        load_m11_baseline(corpus, resolve_m11_baseline_path())
    except ValueError:
        reasons.append("verified_m10_baseline_unavailable")
    return {
        "status": "blocked_pending_operator_input",
        "prepared_at": datetime.now(UTC).isoformat(),
        "measurement_provenance": "not_run",
        "quality_metrics": None,
        "usage": None,
        "blocking_reasons": reasons,
        "models": {"summary": None},
        "summary": {},
        "outcomes": [],
        "corpus_queries": len(corpus["queries"]),
        "planned_search_requests": len(corpus["queries"]) * len(M11_MODES) * len(M11_SUMMARY_AXES) * 2,
        "question_style_queries": {split: sum(query["category"] == "question_style" and query["split"] == split for query in corpus["queries"]) for split in ("dev", "holdout")},
        "minimum_summary_calls": sum(len(segment["content"]) >= KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS for document in corpus["documents"] for segment in document["segments"]),
        "gates": {"quality_passed": False, "all_passed": False, "checks": {}},
        "deployment_note": "仅准备隔离评测入口；未授权处置操作者目标数据库。",
    }


def write_m11_report(report: dict[str, Any], *, json_path: Path = M11_REPORT_JSON_PATH, md_path: Path = M11_REPORT_MD_PATH) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    blocked = report["status"] == "blocked_pending_operator_input"
    retrieval_not_evaluated = not blocked and report.get("retrieval_quality_evaluated", bool(report.get("outcomes"))) is False
    lines = [
        "# M11 质量评测",
        "",
        f"状态：`{report['status']}`",
        "",
        "未调用真实模型；没有实测召回率、nDCG、延迟或费用，F02 质量验收尚未完成。" if blocked else "以下结果来自明确授权的真实 Provider 评测；费用仅在提供经确认的价格时估算。",
        "",
        f"语料问题数：{report['corpus_queries']}；问题式查询：{report.get('question_style_queries', {})}。",
        "",
    ]
    if retrieval_not_evaluated:
        lines += ["摘要生成未完成，未执行检索质量评测；不能据此判断召回质量下降。", ""]
    if blocked:
        lines += [
            f"首次生成至少需要 {report['minimum_summary_calls']} 次摘要调用；重试同样计入授权总预算。",
            f"另外计划执行 {report['planned_search_requests']} 次检索请求（包含重复查询缓存验证）；源文本嵌入、摘要嵌入和 Rerank 同样可能计费，不包含在摘要调用次数上限内。",
            "",
            "执行前需确认摘要模型和最大摘要调用次数，并显式设置 `ACT_WEAVE_KNOWLEDGE_M11_QUALITY_EVAL=1`、`ACT_WEAVE_KNOWLEDGE_M11_SUMMARY_MODEL`、`ACT_WEAVE_KNOWLEDGE_M11_MAX_SUMMARY_CALLS`。",
            "`ACT_WEAVE_KNOWLEDGE_M11_SUMMARY_MODEL` 填 Provider 模型名称；评测会在隔离库中创建独立的 System Model 与加密 Provider 绑定。",
            "",
            "当前没有可验证的 M10 基线报告。" if "verified_m10_baseline_unavailable" in report["blocking_reasons"] else "已找到格式、模型和原题目范围匹配的操作者 M10 基线报告。",
            "操作者可提供历史验证报告，并用 `ACT_WEAVE_KNOWLEDGE_M11_BASELINE_REPORT` 指定 JSON 路径；或另行明确授权新 M10 等价基线运行。",
            "新基线入口保留原 65 题、20 份文档及其冻结内容，报告标注 `fresh_m10_equivalent`，不能当作历史实测。禁止将扩充后的 85 题结果冒充 M10 基线。",
            "新基线使用现有 M10 显式入口 `ACT_WEAVE_KNOWLEDGE_QUALITY_EVAL=1` 和 `test_m10_holdout_quality_gates_against_real_models`；它也调用真实模型，必须单独取得授权。",
            "模型、预算、语料和基线的模型/题数/验收分类指标检查发生在所有 Provider 调用之前。系统密钥使用环境中的现有主密钥，禁止改写目标库。",
            "M11 同时比较摘要开关两组以及冻结 M10 的无答案误召回率和各原分类 P95；延迟超过 1.2 倍只能由操作者通过 `ACT_WEAVE_KNOWLEDGE_M11_P95_REVIEW` 显式记录评审。价格未获确认时不估算费用。",
            "",
        ]
    else:
        lines += [
            "## 验收集",
            "",
            "```json",
            json.dumps(report["summary"].get("holdout", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## 门禁",
            "",
            "```json",
            json.dumps(report["gates"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## 调用和性能",
            "",
            "```json",
            json.dumps(report["usage"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## 摘要调用诊断",
            "",
            "仅含白名单类型、状态、长度和计数。调用序号表示获准的摘要调用尝试，不表示成功 HTTP 请求数。",
            "",
            "```json",
            json.dumps(report.get("summary_diagnostics", {"events": []}), ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    lines += [report["deployment_note"], ""]
    md_path.write_text("\n".join(lines), encoding="utf-8")


async def run_m11_quality_eval(
    postgres_database_url: str,
    *,
    api_key: str,
    summary_model: str | None,
    max_summary_calls: int | None,
    latency_review: str | None = None,
    summary_api_key: str | None = None,
    summary_base_url: str = PROVIDER_BASE_URL,
    approved_prices: dict[str, Any] | None = None,
    baseline_report_path: Path | None = None,
    summary_model_template: CreateSystemModel | None = None,
) -> dict[str, Any]:
    corpus = load_corpus()
    m11_eval_preflight(
        corpus,
        summary_model=summary_model,
        max_summary_calls=max_summary_calls,
        opted_in=os.environ.get("ACT_WEAVE_KNOWLEDGE_M11_QUALITY_EVAL") == "1",
        summary_model_template=summary_model_template,
        summary_base_url=summary_base_url,
    )
    summary_template = _validated_m11_summary_template(summary_model, base_url=summary_base_url, template=summary_model_template)
    baseline_path = resolve_m11_baseline_path(baseline_report_path)
    baseline = load_m11_baseline(corpus, baseline_path)
    # The dispatch body is intentionally separate from preflight so source
    # embeddings cannot happen before all operator inputs have been validated.
    return await _run_m11_authorized(
        postgres_database_url,
        api_key=api_key,
        summary_model=summary_model,
        max_summary_calls=max_summary_calls,
        latency_review=latency_review,
        summary_api_key=summary_api_key,
        summary_base_url=summary_base_url,
        approved_prices=approved_prices,
        corpus=corpus,
        baseline=baseline,
        summary_model_template=summary_template,
    )


async def _configure_m11_summary_model(ctx: EvalContext, *, provider_model: str, base_url: str, api_key: str, summary_model_template: CreateSystemModel | None = None, diagnostics: SummaryDiagnostics | None = None) -> tuple[Any, uuid.UUID]:
    """Summary secrets use the real host master key, independently of legacy retrieval seeds."""
    from types import SimpleNamespace

    from app.audit.models import resolve_system_audit_context
    from app.knowledge.model_port import RegistryKnowledgeModelPort
    from app.knowledge.summary_runtime import DatabaseKnowledgeSummaryRuntime
    from app.system_settings.service import SystemModelCatalogService
    from deerflow.config.app_config import AppConfig
    from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
    from deerflow.persistence.user.model import UserRow
    from deerflow.secrets import SecretKey

    template = _validated_m11_summary_template(provider_model, base_url=base_url, template=summary_model_template)
    admin_id, provider_id = uuid.uuid4(), uuid.uuid4()
    envelope = protect_provider_api_key(provider_id=provider_id, base_url=base_url, api_key=api_key, key=SecretKey.from_environment())
    async with ctx.factory() as session, session.begin():
        session.add(UserRow(id=str(admin_id), email="m11-summary-admin@example.invalid", username="m11_summary_admin", system_role="system_admin", needs_setup=False, token_version=1))
        session.add(ModelProviderRow(id=provider_id, name="M11 Summary Provider", base_url=base_url, request_timeout_seconds=120, api_key_nonce=envelope.nonce, api_key_ciphertext=envelope.ciphertext))
    context = resolve_system_audit_context(SimpleNamespace(id=admin_id, system_role="system_admin"), request_id="m11-quality-summary-model")
    model = await SystemModelCatalogService(ctx.factory).create_model(
        context,
        replace(
            template,
            display_name="M11 Summary Evaluation",
            status="active",
            provider_id=provider_id,
        ),
    )
    async with ctx.factory() as session, session.begin():
        settings = await session.get(KnowledgeSystemSettingsRow, 1)
        if settings is None:
            settings = KnowledgeSystemSettingsRow(id=1)
            session.add(settings)
        settings.summary_model_name = str(model.id)
    config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})
    runtime = DatabaseKnowledgeSummaryRuntime(app_config=config, session_factory=ctx.factory)
    observed_runtime = ObservedSummaryRuntime(runtime, diagnostics) if diagnostics is not None else runtime
    # Existing M10 retrieval fixtures use a deterministic key. Only retrieval
    # material uses it; DatabaseKnowledgeSummaryRuntime decrypts via env master.
    return RegistryKnowledgeModelPort(secret_key=registry_secret_key(), model_runtime=observed_runtime), model.id


async def _generate_m11_summaries(ctx: EvalContext, port: BudgetedSummaryPort) -> dict[str, Any]:
    from actweave_knowledge import KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS, KnowledgeBaseUpdate, KnowledgeSettings
    from actweave_knowledge.bases import KnowledgeBaseService
    from actweave_knowledge.ingestion.summarize import KnowledgeSummarizeHandler
    from actweave_knowledge.persistence.models import KnowledgeSegmentSummaryRow, KnowledgeTaskRow
    from actweave_knowledge.tasks import KnowledgeTaskWorker

    from app.knowledge.composition import is_knowledge_project_active

    async with ctx.factory() as session:
        documents = (
            await session.execute(
                select(KnowledgeDocumentRow.id, KnowledgeDocumentRow.name, func.count(KnowledgeSegmentRow.id), func.count(KnowledgeSegmentRow.id).filter(func.length(KnowledgeSegmentRow.content) >= KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS))
                .outerjoin(KnowledgeSegmentRow, KnowledgeSegmentRow.knowledge_document_id == KnowledgeDocumentRow.id)
                .where(KnowledgeDocumentRow.project_id == ctx.project_id)
                .group_by(KnowledgeDocumentRow.id, KnowledgeDocumentRow.name)
            )
        ).all()
    base_service = KnowledgeBaseService(session_factory=ctx.factory, settings=KnowledgeSettings(enabled=False), model_port=port)
    for base_id in ctx.bases.values():
        await base_service.update_knowledge_base(ctx.project_id, base_id, KnowledgeBaseUpdate(summary_index_enabled=True))
    handler = KnowledgeSummarizeHandler(session_factory=ctx.factory, model_client=ctx.client, model_port=port, project_active_check=is_knowledge_project_active)

    async def counted_handler(claim):
        port.document_id = str(claim.resource_id)
        port.diagnostics.task_id, port.diagnostics.document_id = claim.id, claim.resource_id
        port.diagnostics.task_attempt = claim.attempt_count
        try:
            await handler(claim)
        except BaseException as exc:
            port.diagnostics.record("task_attempt_error", **_safe_summary_exception(exc))
            raise
        finally:
            port.document_id = None
            port.diagnostics.task_id = port.diagnostics.document_id = None
            port.diagnostics.task_attempt = None

    worker = KnowledgeTaskWorker(
        session_factory=ctx.factory, handlers={"summarize_document": counted_handler}, project_active_check=is_knowledge_project_active, concurrency=1, task_timeout_seconds=7200, lease_seconds=60, retry_delay_seconds=0
    )
    while await worker._run_once():
        pass
    async with ctx.factory() as session:
        summary_rows = int(await session.scalar(select(func.count()).select_from(KnowledgeSegmentSummaryRow).where(KnowledgeSegmentSummaryRow.project_id == ctx.project_id)) or 0)
        failed_tasks = int(
            await session.scalar(select(func.count()).select_from(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == ctx.project_id, KnowledgeTaskRow.kind == "summarize_document", KnowledgeTaskRow.status == "failed")) or 0
        )
    return {
        "summary_rows": summary_rows,
        "failed_tasks": failed_tasks,
        "eligible_segments": sum(int(eligible) for _id, _name, _total, eligible in documents),
        "documents": [
            {"document_id": str(doc_id), "source_id": name, "eligible_segments": int(eligible), "skipped_short_segments": int(total - eligible), "llm_calls": port.calls_by_document.get(str(doc_id), 0)}
            for doc_id, name, total, eligible in documents
        ],
    }


async def _run_m11_authorized(
    postgres_database_url: str,
    *,
    api_key: str,
    summary_model: str,
    max_summary_calls: int,
    latency_review: str | None,
    summary_api_key: str | None,
    summary_base_url: str,
    approved_prices: dict[str, Any] | None,
    corpus: dict[str, Any],
    baseline: dict[str, Any],
    summary_model_template: CreateSystemModel,
) -> dict[str, Any]:
    from dataclasses import asdict

    from actweave_knowledge.retrieval.query_cache import KnowledgeQueryEmbeddingCache
    from sqlalchemy import update
    from sqlalchemy.engine import make_url

    from deerflow.secrets import SecretKey

    database_name = make_url(postgres_database_url).database
    if not database_name or not database_name.startswith("deerflow_test_"):
        raise ValueError("M11 evaluation requires an isolated deerflow_test_* database")
    SecretKey.from_environment()  # validate before even the first source embedding
    if not api_key.strip() or not (summary_api_key or api_key).strip():
        raise ValueError("M11 evaluation requires explicit provider credentials")
    # Price validation, if requested, must also happen before paid operations.
    if approved_prices is not None:
        m11_cost_estimate({"embed_tokens_by_model": {PRIMARY_EMBEDDING: 0, SECONDARY_EMBEDDING: 0}, "rerank_tokens": 0, "summary_input_tokens_estimated": 0, "summary_output_tokens_estimated": 0}, approved_prices)
    baseline_digest = content_digest(json.dumps(baseline, ensure_ascii=False, sort_keys=True))
    ctx = await build_eval_context(postgres_database_url, api_key, corpus=corpus)
    engine = ctx.factory.kw["bind"]
    try:
        diagnostics = SummaryDiagnostics()
        delegate, summary_id = await _configure_m11_summary_model(ctx, provider_model=summary_model, base_url=summary_base_url, api_key=summary_api_key or api_key, summary_model_template=summary_model_template, diagnostics=diagnostics)
        port = BudgetedSummaryPort(delegate, max_calls=max_summary_calls, diagnostics=diagnostics)
        generation = await _generate_m11_summaries(ctx, port)
        ctx.service = EvalSearchService(session_factory=ctx.factory, client=ctx.client, model_port=registry_model_port(), query_cache=KnowledgeQueryEmbeddingCache(enabled=True, max_entries=2048, ttl_seconds=86400))
        outcomes: list[QueryOutcome] = []
        cache_pairs: list[dict[str, Any]] = []
        for enabled in (False, True) if generation["failed_tasks"] == 0 else ():
            async with ctx.factory() as session, session.begin():
                await session.execute(update(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == ctx.project_id).values(summary_index_enabled=enabled))
            for mode in M11_MODES:
                for query in ctx.corpus["queries"]:
                    measured = await run_query(ctx, query, mode=mode)
                    measured.summary_enabled = enabled
                    warm = await run_query(ctx, query, mode=mode)
                    outcomes.append(measured)
                    cache_pairs.append(
                        {
                            "query_id": query["id"],
                            "split": query["split"],
                            "mode": mode,
                            "summary_enabled": enabled,
                            "identical_hit_identities": measured.error is None and warm.error is None and measured.hit_ids == warm.hit_ids,
                            "warm_cache_hits": warm.query_embedding_cache_hits,
                            "warm_cache_misses": warm.query_embedding_cache_misses,
                            "error": measured.error or warm.error,
                        }
                    )
        summary = summarize_m11(outcomes)
        gates = evaluate_m11_gates(summary, ctx.corpus, baseline_report=baseline, latency_review=latency_review)
        generation_passed = generation["failed_tasks"] == 0 and generation["summary_rows"] == generation["eligible_segments"]
        cache_passed = bool(cache_pairs) and all(pair["identical_hit_identities"] and pair["warm_cache_hits"] > 0 and pair["warm_cache_misses"] == 0 for pair in cache_pairs)
        gates["checks"]["summary_generation"] = {"passed": generation_passed}
        gates["checks"]["query_cache_pairs"] = {"passed": cache_passed}
        gates["quality_passed"] &= generation_passed and cache_passed
        gates["all_passed"] &= generation_passed and cache_passed
        usage = {
            "embed_calls": ctx.client.embed_calls,
            "embed_tokens_by_model": dict(ctx.client.embed_tokens_by_model),
            "rerank_calls": ctx.client.rerank_calls,
            "rerank_tokens": ctx.client.rerank_tokens,
            "summary_calls": port.calls,
            "summary_call_budget": max_summary_calls,
            "summary_input_tokens_estimated": port.input_tokens_estimated,
            "summary_output_tokens_estimated": port.output_tokens_estimated,
        }
        usage["estimated_cost"] = m11_cost_estimate(usage, approved_prices)
        report = {
            "status": "passed" if gates["all_passed"] else "failed_or_review_pending",
            "ran_at": datetime.now(UTC).isoformat(),
            "measurement_provenance": "authorized_provider_run",
            "hardware": f"{platform.system()} {platform.release()} {platform.machine()}",
            "models": {
                "primary_embedding": PRIMARY_EMBEDDING,
                "secondary_embedding": SECONDARY_EMBEDDING,
                "reranker": RERANKER,
                "summary": summary_model,
                "summary_provider_adapter": summary_model_template.provider_adapter,
                "summary_system_model_id": str(summary_id),
            },
            "m10_baseline_canonical_sha256": baseline_digest,
            "m10_baseline_kind": baseline.get("baseline_kind", "operator_supplied_historical_report"),
            "latency_baseline": "same-run summary-off overall P95 and verified M10 per-category holdout P95 with matching models",
            "corpus_queries": len(ctx.corpus["queries"]),
            "question_style_queries": {split: sum(query["category"] == "question_style" and query["split"] == split for query in ctx.corpus["queries"]) for split in ("dev", "holdout")},
            "retrieval_units": await count_retrieval_units(ctx),
            "summary": summary,
            "quality_metrics": summary.get("holdout"),
            "retrieval_quality_evaluated": bool(outcomes),
            "outcomes": [asdict(row) for row in outcomes],
            "cache_pairs": cache_pairs,
            "summary_generation": generation,
            "summary_diagnostics": {"events": diagnostics.events},
            "usage": usage,
            "gates": gates,
            "deployment_note": "本次仅使用随机隔离评测库。目标库部署、旧数据处置与重置仍需独立确认。",
        }
        write_m11_report(report, json_path=M11_REPORT_JSON_PATH, md_path=M11_REPORT_MD_PATH)
        return report
    finally:
        await ctx.client._inner.aclose()
        await engine.dispose()
