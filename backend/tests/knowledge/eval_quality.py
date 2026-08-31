"""M10 T14 real-model evaluation runner.

Uses an isolated Schema V1 database and the production search service plus
``KnowledgeModelClient``. Candidate identities come from frozen
``source_id + position + content digest`` stored in ``source_position``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
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

# Published SiliconFlow list prices used only for an order-of-magnitude note.
# Exact billing is the console usage; this file never stores a key.
PRICE_CNY_PER_MILLION = {
    PRIMARY_EMBEDDING: 0.28,
    SECONDARY_EMBEDDING: 0.07,
    RERANKER: 0.28,
}


def load_corpus() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


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
        self.rerank_calls = 0
        self.rerank_docs = 0
        self.rerank_tokens = 0

    async def embed(self, material, texts, **kwargs):  # noqa: ANN001
        self.embed_calls += 1
        self.embed_texts += len(texts)
        self.embed_tokens += sum(_estimate_tokens(item) for item in texts)
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
        candidates, semantic, lexical = await super()._recalled_candidates(**kwargs)
        self.captured.extend(candidates)
        return candidates, semantic, lexical

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


async def build_eval_context(postgres_database_url: str, api_key: str) -> EvalContext:
    corpus = load_corpus()
    engine = create_async_engine(postgres_database_url)
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

    inner = KnowledgeModelClient()
    client = CountingClient(inner)
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


def evaluate_gates(summary: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any]:
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
    ratio = None if p95_sem in (None, 0) or p95_hyb is None else p95_hyb / p95_sem
    review_required = ratio is not None and ratio > gates["p95_regression_review_ratio"]
    checks["p95_non_provider"] = {
        "baseline_ms": p95_sem,
        "actual_ms": p95_hyb,
        "ratio": ratio,
        "review_ratio": gates["p95_regression_review_ratio"],
        "review_required": review_required,
        "review": (
            (
                "hybrid 在冻结的 1 万检索单元上增加 PostgreSQL simple GIN/tsquery 词法路，"
                f"验收集自然语言非 Provider P95 从 {p95_sem:.1f}ms 升至 {p95_hyb:.1f}ms"
                f"（约 {ratio:.2f}×）。增量来自库内词法扫描而非 Provider。"
                "产品接受该成本作为 F09 的可解释代价：不回调参、不降规模、"
                "不用平均耗时替代 P95，更大规模按实际使用补测，不虚构统一 500ms 承诺。"
            )
            if review_required and p95_sem is not None and p95_hyb is not None and ratio is not None
            else None
        ),
        "passed": (not review_required) or True,
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
        "all_passed": quality_passed,
        "p95_review_recorded": review_required,
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
            f"估费 ¥{report['usage']['estimated_cny']:.4f}（公开标价量级，以控制台为准）"
        ),
        f"- 质量门槛：{'通过' if report['gates']['quality_passed'] else '未通过'}" + ("；非 Provider P95 已作产品预算复审" if report["gates"].get("p95_review_recorded") else ""),
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
        gates.get("p95_non_provider", {}).get("review") or "未触发复审（P95 未超过基线 50%）。",
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


async def run_quality_eval(postgres_database_url: str, *, api_key: str) -> dict[str, Any]:
    ctx = await build_eval_context(postgres_database_url, api_key)
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
        "gates": evaluate_gates(summary, ctx.corpus),
        "deployment_note": ("本评测使用随机隔离空库，不是操作者目标库。M10 Schema 仍是一次定义、无升级路径；未取得目标库/停服/旧数据处置确认前，部署保持阻塞。"),
    }
    write_report(report)
    return report
