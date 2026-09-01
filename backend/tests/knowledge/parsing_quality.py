"""Test-only baseline/candidate parsing-quality adapter for P4-T6."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

BASELINE_COMMIT = "b96581974b057c0ae4d853815130d99c0ed23823"
_CATEGORIES = {
    "missing_header_column",
    "leading_zero_identifier",
    "long_table_cross_chunk",
    "word_heading_steps",
    "markdown_generic_literal",
    "image_only_no_answer",
}


def load_parsing_cases(path: Path) -> dict[str, Any]:
    """Load and validate the fixed source-grounded quality set."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("baseline_commit") != BASELINE_COMMIT:
        raise ValueError("invalid parsing-quality fixture identity")
    if payload.get("retrieval") != {"mode": "hybrid", "top_k": 5, "score_threshold": 0.0}:
        raise ValueError("parsing-quality retrieval settings must stay frozen")
    documents = payload.get("documents")
    queries = payload.get("queries")
    if not isinstance(documents, list) or not isinstance(queries, list):
        raise ValueError("invalid parsing-quality fixture")
    categories = {item.get("category") for item in documents if isinstance(item, dict)}
    if categories != _CATEGORIES or len({item.get("source_id") for item in documents}) != len(documents):
        raise ValueError("parsing-quality fixture must contain the six fixed sources")
    for category in _CATEGORIES:
        rows = [item for item in queries if item.get("category") == category]
        if len(rows) < 3:
            raise ValueError("each parsing-quality category requires at least three queries")
        for row in rows:
            no_answer = category == "image_only_no_answer"
            if row.get("expected_no_answer") is not no_answer:
                raise ValueError("invalid no-answer classification")
            labels = row.get("relevance")
            if no_answer:
                if labels != []:
                    raise ValueError("no-answer queries cannot carry relevance labels")
                continue
            if not isinstance(labels, list) or not labels:
                raise ValueError("answer queries require source-grounded labels")
            for label in labels:
                if set(label) != {"source_id", "location", "must_contain"}:
                    raise ValueError("invalid source-grounded relevance label")
                if not isinstance(label["location"], dict) or not label["location"] or not isinstance(label["must_contain"], str) or not label["must_contain"]:
                    raise ValueError("incomplete source-grounded relevance label")
    return payload


def relevance_identity(label: dict[str, Any]) -> str:
    """Stable identity from the original source, location and required text."""

    canonical = json.dumps(label, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def segment_relevance_ids(
    segment: dict[str, Any],
    source_id: str,
    labels: list[dict[str, Any]],
) -> list[str]:
    """Map one derived segment to original-source labels without chunk ids."""

    spans = segment.get("source_spans") or []
    locations = [span.get("location") or {} for span in spans if span.get("role", "source") == "source"]
    if not locations:
        locations = [segment.get("source_position") or {}]
    result = []
    for label in labels:
        expected = label["location"]
        location_matches = any(all(location.get(key) == value for key, value in expected.items()) for location in locations)
        if label["source_id"] == source_id and location_matches and label["must_contain"] in segment["content"]:
            result.append(relevance_identity(label))
    return result


def paired_quality_comparison(observations: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Compare only query ids with valid source labels in both corpora."""

    from eval_metrics import mean_or_none

    baseline = {row["id"]: row for row in observations["baseline"]}
    candidate = {row["id"]: row for row in observations["candidate"]}

    def eligible(query_id: str) -> bool:
        left = baseline[query_id]
        right = candidate[query_id]
        return left["error"] is None and right["error"] is None and left["source_labels_available"] and right["source_labels_available"]

    def summarize(query_ids: list[str]) -> dict[str, Any]:
        paired = [query_id for query_id in query_ids if eligible(query_id)]
        baseline_hit = mean_or_none([1.0 if baseline[query_id]["hit_at_5"] else 0.0 for query_id in paired])
        candidate_hit = mean_or_none([1.0 if candidate[query_id]["hit_at_5"] else 0.0 for query_id in paired])
        baseline_mrr = mean_or_none([baseline[query_id]["mrr_at_5"] for query_id in paired])
        candidate_mrr = mean_or_none([candidate[query_id]["mrr_at_5"] for query_id in paired])
        return {
            "paired_queries": len(paired),
            "baseline_hit_at_5": baseline_hit,
            "candidate_hit_at_5": candidate_hit,
            "hit_at_5_change": candidate_hit - baseline_hit if candidate_hit is not None and baseline_hit is not None else None,
            "baseline_mrr_at_5": baseline_mrr,
            "candidate_mrr_at_5": candidate_mrr,
            "mrr_at_5_change": candidate_mrr - baseline_mrr if candidate_mrr is not None and baseline_mrr is not None else None,
        }

    common_ids = sorted(set(baseline) & set(candidate))
    categories = sorted({row["category"] for row in (*baseline.values(), *candidate.values())})
    return {
        "overall": summarize(common_ids),
        "categories": {category: summarize([query_id for query_id in common_ids if baseline[query_id]["category"] == category and candidate[query_id]["category"] == category]) for category in categories},
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _digest_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _candidate_parser_source_digest() -> str:
    """Digest the uncommitted parser/splitter bytes used by this candidate."""

    package = _repo_root() / "backend" / "packages" / "knowledge" / "actweave_knowledge"
    paths = [
        *(path for path in (package / "extraction").rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"),
        *(package / "ingestion" / name for name in ("cleaner.py", "index_text.py", "profiles.py", "source_mapping.py", "splitter.py", "structure.py", "tokenizer.py")),
    ]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(package)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_baseline_export(destination: Path) -> Path:
    """Export the fixed baseline commit without importing it into this process."""

    destination = destination.resolve()
    marker = destination / ".baseline-commit"
    if marker.is_file():
        if marker.read_text(encoding="utf-8").strip() != BASELINE_COMMIT:
            raise ValueError("baseline export has the wrong commit")
        return destination
    if destination.exists():
        raise ValueError("baseline export destination must be empty")

    repository = _repo_root()
    resolved = subprocess.run(
        ["git", "rev-parse", f"{BASELINE_COMMIT}^{{commit}}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if resolved != BASELINE_COMMIT:
        raise ValueError("fixed baseline commit is unavailable")

    destination.mkdir(parents=True, mode=0o700)
    archive_path = destination.parent / f".{destination.name}-{os.getpid()}.tar"
    try:
        with archive_path.open("wb") as archive:
            subprocess.run(
                ["git", "archive", BASELINE_COMMIT, "backend/packages/knowledge"],
                cwd=repository,
                check=True,
                stdout=archive,
            )
        with tarfile.open(archive_path) as archive:
            archive.extractall(destination, filter="data")
    finally:
        archive_path.unlink(missing_ok=True)
    marker.write_text(BASELINE_COMMIT + "\n", encoding="utf-8")
    marker.chmod(0o600)
    return destination


def _materialize_sources(cases: dict[str, Any], cases_path: Path, work_path: Path) -> dict[str, Path]:
    source_dir = work_path / "sources"
    source_dir.mkdir(parents=True, mode=0o700)
    repository = next(parent for parent in cases_path.resolve().parents if (parent / ".git").exists())
    result: dict[str, Path] = {}
    for document in cases["documents"]:
        source = document["source"]
        target = source_dir / source["name"]
        kind = source["kind"]
        if kind == "inline_text":
            data = source["text"].encode("utf-8")
        elif kind == "base64":
            data = base64.b64decode(source["data"], validate=True)
        elif kind == "repo_path":
            original = (repository / source["path"]).resolve(strict=True)
            if not original.is_relative_to(repository):
                raise ValueError("quality source escapes the repository")
            data = original.read_bytes()
        else:
            raise ValueError("unsupported quality source kind")
        digest = hashlib.sha256(data).hexdigest()
        if source.get("sha256") not in {None, digest}:
            raise ValueError("quality source digest mismatch")
        target.write_bytes(data)
        result[document["source_id"]] = target
    return result


_BASELINE_SCRIPT = r"""
import hashlib
import json
import sys
from pathlib import Path

from actweave_knowledge import KnowledgeError
from actweave_knowledge.ingestion.cleaner import clean_blocks
from actweave_knowledge.ingestion.extractor import extract_blocks
from actweave_knowledge.ingestion.splitter import split_blocks

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
result = {"segments": [], "source_sha256": {}, "indexed_segments_by_source": {}}
for document in request["documents"]:
    path = Path(document["path"])
    source_id = document["source_id"]
    result["source_sha256"][source_id] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        blocks = extract_blocks(path, Path(document["original_name"]).suffix.lower(), max_total_chars=5_000_000)
        cleaned = clean_blocks(blocks, remove_extra_spaces=False, remove_urls_emails=False)
        drafts = split_blocks(
            cleaned,
            chunk_size=request["chunking"]["size"],
            chunk_overlap=request["chunking"]["overlap"],
            separator=request["chunking"]["separator"],
        )
    except KnowledgeError:
        if not document["expected_no_text"]:
            raise
        drafts = []
    result["indexed_segments_by_source"][source_id] = len(drafts)
    for draft in drafts:
        result["segments"].append({
            "source_id": source_id,
            "position": draft.position,
            "content": draft.content,
            "index_text": draft.content,
            "source_position": draft.source_position,
            "source_spans": [],
        })
Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
"""


def _baseline_corpus(
    baseline_path: Path,
    cases: dict[str, Any],
    sources: dict[str, Path],
    work_path: Path,
) -> dict[str, Any]:
    request = {
        "chunking": cases["chunking"],
        "documents": [
            {
                "source_id": document["source_id"],
                "path": str(sources[document["source_id"]]),
                "original_name": document["source"]["name"],
                "expected_no_text": document["category"] == "image_only_no_answer",
            }
            for document in cases["documents"]
        ],
    }
    request_path = work_path / "baseline-request.json"
    result_path = work_path / "baseline-result.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(baseline_path / "backend" / "packages" / "knowledge")
    subprocess.run(
        [sys.executable, "-c", _BASELINE_SCRIPT, str(request_path), str(result_path)],
        cwd=baseline_path / "backend",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["commit"] = BASELINE_COMMIT
    result["profile"] = {"unit": "character", **cases["chunking"]}
    return result


def _candidate_corpus(
    cases: dict[str, Any],
    sources: dict[str, Path],
    work_path: Path,
) -> dict[str, Any]:
    from actweave_knowledge import KnowledgeSettings
    from actweave_knowledge.extraction.contracts import (
        ExtractionContext,
        ExtractionLimits,
        ExtractSetting,
        HeaderRule,
    )
    from actweave_knowledge.extraction.images import LocalAttachmentSink
    from actweave_knowledge.extraction.normalizer import normalize_documents
    from actweave_knowledge.extraction.processor import ExtractProcessor
    from actweave_knowledge.extraction.registry import default_registry
    from actweave_knowledge.ingestion.profiles import ProcessingParameters, resolve_processing_profile
    from actweave_knowledge.ingestion.splitter import split_documents

    registry = default_registry()
    settings = KnowledgeSettings(enabled=False, etl_type="dify")
    result: dict[str, Any] = {
        "segments": [],
        "source_sha256": {},
        "indexed_segments_by_source": {},
        "profiles": {},
    }
    for document in cases["documents"]:
        source_id = document["source_id"]
        path = sources[source_id]
        parameters = ProcessingParameters(
            size=cases["chunking"]["size"],
            overlap=cases["chunking"]["overlap"],
            separator=cases["chunking"]["separator"],
            header_rules=tuple(HeaderRule(**item) for item in document["header_rules"]),
        )
        profile = resolve_processing_profile(settings, parameters, registry, extension=path.suffix)
        extraction_dir = work_path / "candidate" / source_id
        extraction_dir.mkdir(parents=True, mode=0o700)
        limits = ExtractionLimits()
        documents = normalize_documents(
            ExtractProcessor(registry).extract(
                ExtractSetting(source_path=path, original_name=path.name, profile=profile.parse),
                ExtractionContext(
                    work_dir=extraction_dir,
                    sink=LocalAttachmentSink(extraction_dir, limits),
                    limits=limits,
                    check_cancelled=lambda: None,
                ),
            )
        )
        drafts = split_documents(tuple(documents), profile=profile.chunk)
        result["source_sha256"][source_id] = hashlib.sha256(path.read_bytes()).hexdigest()
        result["indexed_segments_by_source"][source_id] = len(drafts)
        result["profiles"][source_id] = profile.model_dump(mode="json")
        for draft in drafts:
            result["segments"].append(
                {
                    "source_id": source_id,
                    "position": draft.position,
                    "content": draft.content,
                    "index_text": draft.index_text,
                    "source_position": draft.source_position,
                    "source_spans": [span.model_dump(mode="json") for span in draft.source_spans],
                }
            )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_repo_root(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result["revision"] = revision + "+working-tree"
    result["parser_source_digest"] = _candidate_parser_source_digest()
    return result


def _all_labels(cases: dict[str, Any]) -> list[dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    for query in cases["queries"]:
        for label in query["relevance"]:
            labels.setdefault(relevance_identity(label), label)
    return list(labels.values())


def _add_source_assertions(corpus: dict[str, Any], cases: dict[str, Any]) -> None:
    labels = _all_labels(cases)
    matched: set[str] = set()
    for segment in corpus["segments"]:
        matched.update(segment_relevance_ids(segment, segment["source_id"], labels))
    expected = {relevance_identity(label) for label in labels}
    corpus["source_assertions_matched"] = len(matched)
    corpus["source_assertions_missing"] = sorted(expected - matched)


def build_parsing_corpora(
    *,
    baseline_path: Path,
    cases_path: Path,
    work_path: Path,
) -> dict[str, Any]:
    """Run the fixed old and current parsing paths over byte-identical inputs."""

    cases = load_parsing_cases(cases_path)
    baseline_path = prepare_baseline_export(baseline_path)
    work_path.mkdir(parents=True, mode=0o700, exist_ok=True)
    sources = _materialize_sources(cases, cases_path, work_path)
    baseline = _baseline_corpus(baseline_path, cases, sources, work_path)
    candidate = _candidate_corpus(cases, sources, work_path)
    if baseline["source_sha256"] != candidate["source_sha256"]:
        raise ValueError("baseline and candidate did not process identical originals")
    _add_source_assertions(baseline, cases)
    _add_source_assertions(candidate, cases)
    return {"baseline": baseline, "candidate": candidate}


class _ReplayClient:
    """Deterministic model port for workflow assertions; performs no I/O."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    @staticmethod
    def _tokens(text: str) -> list[str]:
        from actweave_knowledge.retrieval.lexical import lexical_v1_tokens

        return lexical_v1_tokens(text)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = self._tokens(text) or [text]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0
        length = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / length for value in vector]

    async def embed(self, material, texts, *, batch_guard=None, on_batch_verified=None):  # noqa: ANN001
        if batch_guard is not None:
            await batch_guard()
        vectors = [self._vector(text) for text in texts]
        if on_batch_verified is not None:
            await on_batch_verified(len(texts))
        return vectors

    async def rerank(self, material, query, documents, top_n, *, batch_guard=None):  # noqa: ANN001
        from actweave_knowledge.models.client import RerankScore

        if batch_guard is not None:
            await batch_guard()
        query_tokens = set(self._tokens(query))
        ranked = []
        for index, document in enumerate(documents):
            document_tokens = set(self._tokens(document))
            union = query_tokens | document_tokens
            score = len(query_tokens & document_tokens) / len(union) if union else 0.0
            ranked.append(RerankScore(index=index, score=score))
        ranked.sort(key=lambda item: (-item.score, item.index))
        return ranked[:top_n]

    async def aclose(self) -> None:
        return None


async def _seed_quality_search(
    postgres_database_url: str,
    *,
    corpora: dict[str, Any],
    cases: dict[str, Any],
    api_key: str | None,
) -> dict[str, Any]:
    from actweave_knowledge import KnowledgeError, KnowledgeSearchRequest
    from actweave_knowledge.models.client import KnowledgeModelClient
    from actweave_knowledge.persistence.models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeSegmentRow
    from actweave_knowledge.retrieval import lexical_index_input
    from eval_metrics import mean_or_none, recall_hit, reciprocal_rank_at_k
    from eval_quality import (
        EMBED_DIMENSION,
        MAX_BATCH,
        PRIMARY_EMBEDDING,
        PROVIDER_BASE_URL,
        REQUEST_TIMEOUT,
        RERANKER,
        CountingClient,
        EvalSearchService,
        _embed_unique,
        _seed_project,
        content_digest,
    )
    from registry_helpers import registry_model_port, seed_embedding_model, seed_provider, seed_rerank_model
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from deerflow.persistence.bootstrap import _install_full_schema
    from deerflow.persistence.model_registry import ModelProviderModelRow, ModelProviderRow

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    inner = KnowledgeModelClient() if api_key is not None else _ReplayClient(EMBED_DIMENSION)
    client = CountingClient(inner)  # type: ignore[arg-type]
    try:
        await _install_full_schema(engine)
        provider_id = await seed_provider(
            factory,
            base_url=PROVIDER_BASE_URL if api_key is not None else "https://provider.invalid/v1",
            request_timeout_seconds=REQUEST_TIMEOUT,
            api_key=api_key or "sk-replay-no-network",
        )
        embedding_id = await seed_embedding_model(
            factory,
            provider_id,
            dimension=EMBED_DIMENSION,
            max_batch=MAX_BATCH,
            model_name=PRIMARY_EMBEDDING,
        )
        reranker_id = await seed_rerank_model(
            factory,
            provider_id,
            max_batch=MAX_BATCH,
            model_name=RERANKER,
        )
        async with factory() as session, session.begin():
            project_id, owner_user_id = await _seed_project(session)
        async with factory() as session, session.begin():
            material = await registry_model_port().embedding_material(session, embedding_id)

        all_index_text = [segment["index_text"] for corpus in corpora.values() for segment in corpus["segments"]]
        vectors = await _embed_unique(client, material, all_index_text)
        bases: dict[str, uuid.UUID] = {}
        segment_labels: dict[str, dict[uuid.UUID, set[str]]] = {"baseline": {}, "candidate": {}}
        labels = _all_labels(cases)
        async with factory() as session, session.begin():
            for variant in ("baseline", "candidate"):
                base_id = uuid.uuid4()
                bases[variant] = base_id
                session.add(
                    KnowledgeBaseRow(
                        id=base_id,
                        project_id=project_id,
                        name=f"Parsing quality {variant}",
                        embedding_model_id=embedding_id,
                        reranker_model_id=reranker_id,
                        retrieval_mode="hybrid",
                        default_top_k=cases["retrieval"]["top_k"],
                        default_score_threshold=cases["retrieval"]["score_threshold"],
                    )
                )
            await session.flush()
            documents_by_variant_source: dict[tuple[str, str], KnowledgeDocumentRow] = {}
            source_specs = {item["source_id"]: item for item in cases["documents"]}
            for variant in ("baseline", "candidate"):
                corpus = corpora[variant]
                for source_id, source_spec in source_specs.items():
                    document_id = uuid.uuid4()
                    profile = None if variant == "baseline" else corpus["profiles"][source_id]
                    row = KnowledgeDocumentRow(
                        id=document_id,
                        project_id=project_id,
                        knowledge_base_id=bases[variant],
                        name=source_id,
                        original_name=source_spec["source"]["name"],
                        storage_key=f"projects/{project_id}/quality/{variant}/{document_id}/{source_spec['source']['name']}",
                        size_bytes=1,
                        status="ready",
                        version=1,
                        published_version=1,
                        chunk_size=cases["chunking"]["size"],
                        chunk_overlap=cases["chunking"]["overlap"],
                        chunk_separator=cases["chunking"]["separator"],
                        source_sha256=corpus["source_sha256"][source_id],
                        parsing_profile=profile,
                        segment_count=corpus["indexed_segments_by_source"][source_id],
                    )
                    session.add(row)
                    documents_by_variant_source[(variant, source_id)] = row
            await session.flush()
            for variant in ("baseline", "candidate"):
                for segment in corpora[variant]["segments"]:
                    source_id = segment["source_id"]
                    document = documents_by_variant_source[(variant, source_id)]
                    segment_id = uuid.uuid4()
                    source_position = {"source_id": source_id, **segment["source_position"]}
                    session.add(
                        KnowledgeSegmentRow(
                            id=segment_id,
                            project_id=project_id,
                            knowledge_base_id=bases[variant],
                            knowledge_document_id=document.id,
                            document_version=1,
                            position=segment["position"],
                            content=segment["content"],
                            index_text=segment["index_text"],
                            token_count=0,
                            word_count=len(segment["content"]),
                            source_position=source_position,
                            source_spans=segment["source_spans"],
                            embedding=vectors[content_digest(segment["index_text"])],
                            lexical_tsv=func.to_tsvector("simple", lexical_index_input(segment["index_text"])),
                            lexical_version=1,
                        )
                    )
                    segment_labels[variant][segment_id] = set(segment_relevance_ids(segment, source_id, labels))

        async with factory() as session:
            provider_row = await session.get(ModelProviderRow, provider_id)
            embedding_row = await session.get(ModelProviderModelRow, embedding_id)
            reranker_row = await session.get(ModelProviderModelRow, reranker_id)
            bound_rows = (await session.scalars(select(KnowledgeBaseRow).where(KnowledgeBaseRow.id.in_(tuple(bases.values()))))).all()
        assert provider_row is not None and embedding_row is not None and reranker_row is not None
        rows_by_id = {row.id: row for row in bound_rows}
        base_bindings = {
            variant: {
                "embedding_model_id": str(rows_by_id[base_id].embedding_model_id),
                "reranker_model_id": str(rows_by_id[base_id].reranker_model_id),
            }
            for variant, base_id in bases.items()
        }
        provider_config = {
            "base_url": provider_row.base_url,
            "request_timeout_seconds": provider_row.request_timeout_seconds,
        }
        embedding_config = {
            "provider_id": str(embedding_row.provider_id),
            "model_type": embedding_row.model_type,
            "model_name": embedding_row.model_name,
            "embedding_dimension": embedding_row.embedding_dimension,
            "max_batch": embedding_row.max_batch,
            "status": embedding_row.status,
        }
        reranker_config = {
            "provider_id": str(reranker_row.provider_id),
            "model_type": reranker_row.model_type,
            "model_name": reranker_row.model_name,
            "embedding_dimension": reranker_row.embedding_dimension,
            "max_batch": reranker_row.max_batch,
            "status": reranker_row.status,
        }

        service = EvalSearchService(
            session_factory=factory,
            client=client,  # type: ignore[arg-type]
            model_port=registry_model_port(),
        )
        answer_queries = [query for query in cases["queries"] if not query["expected_no_answer"]]
        observations: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
        invocations = 0
        for variant in ("baseline", "candidate"):
            for query in answer_queries:
                try:
                    result = await service.search(
                        KnowledgeSearchRequest(
                            project_id=project_id,
                            owner_user_id=owner_user_id,
                            query=query["query"],
                            knowledge_base_ids=(bases[variant],),
                            top_k=cases["retrieval"]["top_k"],
                            score_threshold=cases["retrieval"]["score_threshold"],
                            retrieval_mode="hybrid",
                        )
                    )
                except KnowledgeError as error:
                    invocations += 1
                    observations[variant].append(
                        {
                            "id": query["id"],
                            "category": query["category"],
                            "source_labels_available": False,
                            "returned": 0,
                            "hit_at_5": None,
                            "mrr_at_5": None,
                            "error": error.code,
                        }
                    )
                    continue
                invocations += 1
                targets = [relevance_identity(label) for label in query["relevance"]]
                available = any(target in identities for identities in segment_labels[variant].values() for target in targets)
                ranked: list[str] = []
                for hit in result.hits:
                    identities = segment_labels[variant].get(hit.citation.segment_id, set())
                    ranked.append(next((target for target in targets if target in identities), f"miss:{hit.citation.segment_id}"))
                observations[variant].append(
                    {
                        "id": query["id"],
                        "category": query["category"],
                        "source_labels_available": available,
                        "returned": len(result.hits),
                        "hit_at_5": recall_hit(targets, ranked) if available else None,
                        "mrr_at_5": reciprocal_rank_at_k(targets, ranked, k=5) if available else None,
                        "error": None,
                    }
                )

        def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                grouped[row["category"]].append(row)
            result = {}
            for category, category_rows in grouped.items():
                successful = [row for row in category_rows if row["error"] is None]
                measured = [row for row in successful if row["source_labels_available"]]
                result[category] = {
                    "queries": len(category_rows),
                    "successful": len(successful),
                    "failed": len(category_rows) - len(successful),
                    "measured": len(measured),
                    "source_labels_missing": len(successful) - len(measured),
                    "hit_at_5": mean_or_none([1.0 if row["hit_at_5"] else 0.0 for row in measured]),
                    "mrr_at_5": mean_or_none([row["mrr_at_5"] for row in measured]),
                }
            successful = [row for row in rows if row["error"] is None]
            measured = [row for row in successful if row["source_labels_available"]]
            result["overall"] = {
                "queries": len(rows),
                "successful": len(successful),
                "failed": len(rows) - len(successful),
                "measured": len(measured),
                "source_labels_missing": len(successful) - len(measured),
                "hit_at_5": mean_or_none([1.0 if row["hit_at_5"] else 0.0 for row in measured]),
                "mrr_at_5": mean_or_none([row["mrr_at_5"] for row in measured]),
            }
            return result

        summaries = {variant: summary(rows) for variant, rows in observations.items()}
        comparison = None
        conclusion = "not_evaluated"
        failed_queries = sum(summaries[variant]["overall"]["failed"] for variant in ("baseline", "candidate"))
        if api_key is not None:
            comparison = paired_quality_comparison(observations)
            if failed_queries:
                conclusion = "failed_queries"
            elif summaries["baseline"]["overall"]["source_labels_missing"] or summaries["candidate"]["overall"]["source_labels_missing"]:
                conclusion = "incomplete_source_labels"
            elif comparison["overall"]["mrr_at_5_change"] > 0:
                conclusion = "improved"
            elif comparison["overall"]["mrr_at_5_change"] < 0:
                conclusion = "regressed"
            else:
                conclusion = "unchanged"

        no_answer_queries = [query for query in cases["queries"] if query["expected_no_answer"]]
        return {
            "execution_mode": "provider" if api_key is not None else "replay",
            "quality_comparison": comparison,
            "quality_conclusion": conclusion,
            "retrieval": {
                "implementation": "KnowledgeSearchService",
                **cases["retrieval"],
                "search_invocations": invocations,
            },
            "models": {
                "provider": {
                    "id": str(provider_id),
                    "revision": provider_row.updated_at.isoformat(),
                    "config_digest": _digest_payload(provider_config),
                    "request_timeout_seconds": provider_row.request_timeout_seconds,
                },
                "embedding": {
                    "id": str(embedding_id),
                    "revision": embedding_row.updated_at.isoformat(),
                    "config_digest": _digest_payload(embedding_config),
                    **embedding_config,
                },
                "reranker": {
                    "id": str(reranker_id),
                    "revision": reranker_row.updated_at.isoformat(),
                    "config_digest": _digest_payload(reranker_config),
                    **reranker_config,
                },
                "embedding_model_id": str(embedding_id),
                "embedding_model": PRIMARY_EMBEDDING,
                "embedding_dimension": EMBED_DIMENSION,
                "reranker_model_id": str(reranker_id),
                "reranker_model": RERANKER,
                "base_bindings": base_bindings,
                "same_configuration": len({tuple(binding.values()) for binding in base_bindings.values()}) == 1,
            },
            "provider_calls": 0 if api_key is None else client.embed_calls + client.rerank_calls,
            "failed_queries": failed_queries,
            "answer_queries": len(answer_queries),
            "corpus": {
                "sources": len(cases["documents"]),
                "categories": len({document["category"] for document in cases["documents"]}),
                "answer_queries": len(answer_queries),
                "no_answer_queries": len(no_answer_queries),
                "source_sha256": corpora["baseline"]["source_sha256"],
            },
            "profiles": {
                "baseline": corpora["baseline"]["profile"],
                "candidate": corpora["candidate"]["profiles"],
            },
            "no_answer": {
                "category": "image_only_no_answer",
                "queries": len(no_answer_queries),
                "baseline_indexed_segments": corpora["baseline"]["indexed_segments_by_source"]["image-only"],
                "candidate_indexed_segments": corpora["candidate"]["indexed_segments_by_source"]["image-only"],
                "included_in_answer_means": False,
            },
            "replay_observations": summaries,
            "source_assertions": {
                variant: {
                    "matched": corpora[variant]["source_assertions_matched"],
                    "missing": corpora[variant]["source_assertions_missing"],
                }
                for variant in ("baseline", "candidate")
            },
            "baseline": {"commit": BASELINE_COMMIT},
            "candidate": {
                "revision": corpora["candidate"]["revision"],
                "working_tree_digest": corpora["candidate"]["parser_source_digest"],
            },
        }
    finally:
        await inner.aclose()
        await engine.dispose()


async def run_parsing_quality_eval(
    postgres_database_url: str,
    *,
    baseline_path: Path,
    cases_path: Path,
    api_key: str | None,
) -> dict[str, Any]:
    """Evaluate fixed baseline/candidate corpora; replay never claims quality."""

    cases = load_parsing_cases(cases_path)
    with tempfile.TemporaryDirectory(prefix="actweave-parsing-quality-") as directory:
        corpora = build_parsing_corpora(
            baseline_path=baseline_path,
            cases_path=cases_path,
            work_path=Path(directory),
        )
        return await _seed_quality_search(
            postgres_database_url,
            corpora=corpora,
            cases=cases,
            api_key=api_key,
        )
