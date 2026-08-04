#!/usr/bin/env python3
"""Run the bounded Checkpoint A quality evaluation for Memory v2 extraction."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.private_work.memory_source_admission import (
    MEMORY_EXTRACT_PROMPT_VERSION,
    MEMORY_EXTRACTOR_VERSION,
)
from app.system_settings import SystemModelMaterializer
from deerflow.agents.memory.extractor import (
    MemoryCandidateExtractor,
    MemoryExtractionError,
    MemoryExtractionSource,
    RunOneshotMemoryExtractionModelCaller,
)
from deerflow.config import get_app_config

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = _BACKEND_ROOT / "tests" / "fixtures" / "memory_extractor_checkpoint_a.jsonl"
DEFAULT_REPORT = _BACKEND_ROOT.parent / "docs" / "memory-extractor-checkpoint-a-report.json"

_CANDIDATE_TYPES = frozenset(
    {
        "preference",
        "constraint",
        "correction",
        "context",
        "knowledge",
        "behavior",
        "goal",
    }
)
_LANGUAGES = frozenset({"zh", "en"})
_REQUIRED_CATEGORIES = frozenset(
    {
        "constraint",
        "correction",
        "credential",
        "duplicate",
        "preference",
        "scope_isolation",
        "temporary",
    }
)
_PRECISION_THRESHOLD = 0.95
_RECALL_THRESHOLD = 0.85

_NEGATION_PATTERNS = (
    re.compile(
        r"\b(?:cannot|can't|do not|does not|forbidden|must not|mustn't|never|"
        r"no longer|not|should not|shouldn't)\b",
        re.IGNORECASE,
    ),
    re.compile(r"不得|不能|不再|不可|不要|从不|禁止"),
)


def _normalize(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value).casefold().split(),
    )


class MemoryEvaluationInvalid(ValueError):
    """The fixed evaluation input or produced result is malformed."""


@dataclass(frozen=True, slots=True)
class ExpectedCandidate:
    candidate_type: str
    required_terms: tuple[str, ...]
    direction_terms: tuple[str, ...]
    polarity: Literal["positive", "negative"] = "positive"

    def __post_init__(self) -> None:
        if self.candidate_type not in _CANDIDATE_TYPES:
            raise MemoryEvaluationInvalid("expected candidate type is invalid")
        if not self.required_terms or any(not isinstance(term, str) or not term.strip() for term in self.required_terms):
            raise MemoryEvaluationInvalid("expected candidate terms are invalid")
        if not self.direction_terms or any(not isinstance(term, str) or not term.strip() for term in self.direction_terms):
            raise MemoryEvaluationInvalid("expected candidate direction is invalid")
        normalized_required = tuple(_normalize(term) for term in self.required_terms)
        if any(not all(required in _normalize(direction) for required in normalized_required) for direction in self.direction_terms):
            raise MemoryEvaluationInvalid("expected candidate direction is incomplete")
        if self.polarity not in {"positive", "negative"}:
            raise MemoryEvaluationInvalid("expected candidate polarity is invalid")


@dataclass(frozen=True, slots=True)
class MemoryEvalCase:
    case_id: str
    batch_id: str
    scope_key: str
    language: Literal["zh", "en"]
    category: str
    content: str
    expected: tuple[ExpectedCandidate, ...]
    forbidden_secret_terms: tuple[str, ...] = ()
    forbidden_scope_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        compact = (
            self.case_id,
            self.batch_id,
            self.scope_key,
            self.category,
            self.content,
        )
        if any(not isinstance(value, str) or not value.strip() for value in compact):
            raise MemoryEvaluationInvalid("evaluation case contains an empty field")
        if self.language not in _LANGUAGES:
            raise MemoryEvaluationInvalid("evaluation language is invalid")
        for terms in (
            self.forbidden_secret_terms,
            self.forbidden_scope_terms,
        ):
            if any(not isinstance(term, str) or not term.strip() for term in terms):
                raise MemoryEvaluationInvalid("forbidden term is invalid")


@dataclass(frozen=True, slots=True)
class EvalCandidate:
    source_ordinal: int
    candidate_type: str
    content: str

    def __post_init__(self) -> None:
        if type(self.source_ordinal) is not int or self.source_ordinal < 0 or self.candidate_type not in _CANDIDATE_TYPES or not isinstance(self.content, str) or not self.content.strip():
            raise MemoryEvaluationInvalid("evaluated candidate is invalid")


@dataclass(frozen=True, slots=True)
class EvalBatchResult:
    batch_id: str
    duration_ms: int
    candidates: tuple[EvalCandidate, ...]
    error_code: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.batch_id, str)
            or not self.batch_id
            or type(self.duration_ms) is not int
            or self.duration_ms < 0
            or not isinstance(self.candidates, tuple)
            or any(type(candidate) is not EvalCandidate for candidate in self.candidates)
            or (self.error_code is not None and (not isinstance(self.error_code, str) or not self.error_code or len(self.error_code) > 64))
            or (self.error_code is not None and self.candidates)
        ):
            raise MemoryEvaluationInvalid("evaluation batch result is invalid")


@dataclass(frozen=True, slots=True)
class MemoryEvaluationReport:
    sample_count: int
    batch_count: int
    expected_count: int
    predicted_count: int
    matched_count: int
    false_positive_count: int
    false_negative_count: int
    secret_leak_count: int
    scope_violation_count: int
    duplicate_candidate_count: int
    batch_error_count: int
    total_duration_ms: int
    precision: float
    recall: float
    passed: bool
    candidate_types_by_case_id: dict[str, tuple[str, ...]]
    unmatched_expected_case_ids: tuple[str, ...]
    unexpected_candidate_case_ids: tuple[str, ...]
    error_batch_ids: tuple[str, ...]


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise MemoryEvaluationInvalid(f"{field} must be a list of strings")
    return tuple(item.strip() for item in value)


def _expected_candidates(value: object) -> tuple[ExpectedCandidate, ...]:
    if not isinstance(value, list):
        raise MemoryEvaluationInvalid("expected must be a list")
    candidates: list[ExpectedCandidate] = []
    for raw in value:
        if (
            not isinstance(raw, dict)
            or not {"candidate_type", "direction_terms", "required_terms"}.issubset(raw)
            or not set(raw).issubset(
                {"candidate_type", "direction_terms", "required_terms", "polarity"},
            )
        ):
            raise MemoryEvaluationInvalid("expected candidate shape is invalid")
        candidates.append(
            ExpectedCandidate(
                candidate_type=raw["candidate_type"],
                required_terms=_string_tuple(
                    raw["required_terms"],
                    field="required_terms",
                ),
                direction_terms=_string_tuple(
                    raw["direction_terms"],
                    field="direction_terms",
                ),
                polarity=raw.get("polarity", "positive"),
            )
        )
    return tuple(candidates)


def _parse_case(raw: object) -> MemoryEvalCase:
    required = {
        "batch_id",
        "case_id",
        "category",
        "content",
        "expected",
        "language",
        "scope_key",
    }
    optional = {"forbidden_scope_terms", "forbidden_secret_terms"}
    if not isinstance(raw, dict) or not required.issubset(raw) or not set(raw).issubset(required | optional):
        raise MemoryEvaluationInvalid("evaluation case shape is invalid")
    return MemoryEvalCase(
        case_id=raw["case_id"],
        batch_id=raw["batch_id"],
        scope_key=raw["scope_key"],
        language=raw["language"],
        category=raw["category"],
        content=raw["content"],
        expected=_expected_candidates(raw["expected"]),
        forbidden_secret_terms=_string_tuple(
            raw.get("forbidden_secret_terms"),
            field="forbidden_secret_terms",
        ),
        forbidden_scope_terms=_string_tuple(
            raw.get("forbidden_scope_terms"),
            field="forbidden_scope_terms",
        ),
    )


def load_eval_cases(path: Path) -> tuple[MemoryEvalCase, ...]:
    """Load a strict, ordered JSONL fixture without logging its contents."""

    if not isinstance(path, Path) or not path.is_file():
        raise MemoryEvaluationInvalid("evaluation fixture is unavailable")
    cases: list[MemoryEvalCase] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            cases.append(_parse_case(json.loads(line)))
        except (json.JSONDecodeError, MemoryEvaluationInvalid) as error:
            raise MemoryEvaluationInvalid(
                f"evaluation fixture line {line_number} is invalid",
            ) from error
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise MemoryEvaluationInvalid("evaluation case ids are invalid")
    scope_by_batch: dict[str, str] = {}
    for case in cases:
        previous = scope_by_batch.setdefault(case.batch_id, case.scope_key)
        if previous != case.scope_key:
            raise MemoryEvaluationInvalid("one evaluation batch crosses scopes")
    categories = {case.category for case in cases}
    if (
        len(cases) < 50
        or {case.language for case in cases} != _LANGUAGES
        or len({case.scope_key for case in cases}) < 2
        or len(scope_by_batch) < 2
        or sum(len(case.expected) for case in cases) < 20
        or sum(not case.expected for case in cases) < 10
        or sum(bool(case.forbidden_secret_terms) for case in cases) < 4
        or not categories.issuperset(_REQUIRED_CATEGORIES)
    ):
        raise MemoryEvaluationInvalid("evaluation fixture coverage is invalid")
    return tuple(cases)


def _matches(
    candidate: EvalCandidate,
    expected: ExpectedCandidate,
) -> bool:
    content = _normalize(candidate.content)
    has_negation = any(pattern.search(content) for pattern in _NEGATION_PATTERNS)
    polarity_matches = expected.candidate_type == "correction" or has_negation == (expected.polarity == "negative")
    return candidate.candidate_type == expected.candidate_type and polarity_matches and all(_normalize(term) in content for term in expected.required_terms) and any(_normalize(term) in content for term in expected.direction_terms)


def _group_cases(
    cases: Sequence[MemoryEvalCase],
) -> dict[str, tuple[MemoryEvalCase, ...]]:
    grouped: dict[str, list[MemoryEvalCase]] = defaultdict(list)
    for case in cases:
        grouped[case.batch_id].append(case)
    return {batch_id: tuple(items) for batch_id, items in grouped.items()}


def _term_hits(candidates: Iterable[EvalCandidate], terms: Iterable[str]) -> int:
    normalized_terms = tuple(_normalize(term) for term in terms)
    return sum(1 for candidate in candidates if any(term in _normalize(candidate.content) for term in normalized_terms))


def score_eval_results(
    cases: Sequence[MemoryEvalCase],
    batch_results: Sequence[EvalBatchResult],
) -> MemoryEvaluationReport:
    """Score exact type plus required-term matches without persisting bodies."""

    grouped = _group_cases(cases)
    result_by_batch: dict[str, EvalBatchResult] = {}
    for result in batch_results:
        if result.batch_id in result_by_batch or result.batch_id not in grouped:
            raise MemoryEvaluationInvalid("evaluation batch results are invalid")
        result_by_batch[result.batch_id] = result

    expected_count = sum(len(case.expected) for case in cases)
    predicted_count = 0
    matched_count = 0
    secret_leak_count = 0
    scope_violation_count = 0
    duplicate_count = 0
    batch_error_count = 0
    total_duration_ms = 0
    unmatched_case_ids: list[str] = []
    unexpected_case_ids: list[str] = []
    error_batch_ids: list[str] = []
    candidate_types_by_case_id: dict[str, tuple[str, ...]] = {}

    for batch_id, batch_cases in grouped.items():
        result = result_by_batch.get(batch_id)
        if result is None or result.error_code is not None:
            batch_error_count += 1
            error_batch_ids.append(batch_id)
            unmatched_case_ids.extend(case.case_id for case in batch_cases for _ in case.expected)
            if result is not None:
                total_duration_ms += result.duration_ms
            continue

        total_duration_ms += result.duration_ms
        predicted_count += len(result.candidates)
        seen_contents: set[str] = set()
        for candidate in result.candidates:
            content_key = _normalize(candidate.content)
            if content_key in seen_contents:
                duplicate_count += 1
            seen_contents.add(content_key)

        secret_terms = tuple(term for case in batch_cases for term in case.forbidden_secret_terms)
        scope_terms = (term for case in batch_cases for term in case.forbidden_scope_terms)
        for candidate in result.candidates:
            source_is_secret = 0 <= candidate.source_ordinal < len(batch_cases) and bool(
                batch_cases[candidate.source_ordinal].forbidden_secret_terms,
            )
            content = _normalize(candidate.content)
            if source_is_secret or any(_normalize(term) in content for term in secret_terms):
                secret_leak_count += 1
        scope_violation_count += _term_hits(result.candidates, scope_terms)

        predicted_by_ordinal: dict[int, list[EvalCandidate]] = defaultdict(list)
        for candidate in result.candidates:
            predicted_by_ordinal[candidate.source_ordinal].append(candidate)

        for ordinal, predicted in predicted_by_ordinal.items():
            key = batch_cases[ordinal].case_id if 0 <= ordinal < len(batch_cases) else f"{batch_id}:ordinal:{ordinal}"
            candidate_types_by_case_id[key] = tuple(candidate.candidate_type for candidate in predicted)

        for ordinal, case in enumerate(batch_cases):
            available = list(predicted_by_ordinal.pop(ordinal, ()))
            matched_indices: set[int] = set()
            for expected in case.expected:
                match_index = next(
                    (index for index, candidate in enumerate(available) if index not in matched_indices and _matches(candidate, expected)),
                    None,
                )
                if match_index is None:
                    unmatched_case_ids.append(case.case_id)
                else:
                    matched_indices.add(match_index)
                    matched_count += 1
            unexpected_case_ids.extend(case.case_id for index in range(len(available)) if index not in matched_indices)
        for invalid_ordinal, candidates in predicted_by_ordinal.items():
            unexpected_case_ids.extend(f"{batch_id}:ordinal:{invalid_ordinal}" for _ in candidates)

    false_positive_count = predicted_count - matched_count
    false_negative_count = expected_count - matched_count
    precision = matched_count / predicted_count if predicted_count else (1.0 if expected_count == 0 else 0.0)
    recall = matched_count / expected_count if expected_count else 1.0
    passed = (
        len(cases) >= 50
        and precision >= _PRECISION_THRESHOLD
        and recall >= _RECALL_THRESHOLD
        and secret_leak_count == 0
        and scope_violation_count == 0
        and duplicate_count == 0
        and batch_error_count == 0
        and set(result_by_batch) == set(grouped)
    )
    return MemoryEvaluationReport(
        sample_count=len(cases),
        batch_count=len(grouped),
        expected_count=expected_count,
        predicted_count=predicted_count,
        matched_count=matched_count,
        false_positive_count=false_positive_count,
        false_negative_count=false_negative_count,
        secret_leak_count=secret_leak_count,
        scope_violation_count=scope_violation_count,
        duplicate_candidate_count=duplicate_count,
        batch_error_count=batch_error_count,
        total_duration_ms=total_duration_ms,
        precision=precision,
        recall=recall,
        passed=passed,
        candidate_types_by_case_id=candidate_types_by_case_id,
        unmatched_expected_case_ids=tuple(unmatched_case_ids),
        unexpected_candidate_case_ids=tuple(unexpected_case_ids),
        error_batch_ids=tuple(error_batch_ids),
    )


async def _run_live_evaluation(
    cases: tuple[MemoryEvalCase, ...],
    *,
    model_ref: str | None,
) -> tuple[str, tuple[EvalBatchResult, ...]]:
    app_config = await asyncio.to_thread(get_app_config)
    engine = create_async_engine(
        app_config.database.sqlalchemy_url,
        pool_pre_ping=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        model = await SystemModelMaterializer(factory).materialize_active(
            model_ref,
        )
        runtime_config = app_config.with_runtime_models((model,))
        extractor = MemoryCandidateExtractor(
            RunOneshotMemoryExtractionModelCaller(
                app_config=runtime_config,
                model_name=model.name,
            )
        )
        results: list[EvalBatchResult] = []
        for batch_id, batch_cases in _group_cases(cases).items():
            started = perf_counter()
            try:
                extracted = await extractor.extract(
                    tuple(
                        MemoryExtractionSource(
                            ordinal=ordinal,
                            content=case.content,
                        )
                        for ordinal, case in enumerate(batch_cases)
                    )
                )
                candidates = tuple(
                    EvalCandidate(
                        source_ordinal=candidate.source_ordinal,
                        candidate_type=candidate.candidate_type,
                        content=candidate.content,
                    )
                    for candidate in extracted.candidates
                )
                error_code = None
            except MemoryExtractionError as error:
                candidates = ()
                error_code = error.code
            except Exception:
                candidates = ()
                error_code = "MEMORY_EVAL_UNAVAILABLE"
            results.append(
                EvalBatchResult(
                    batch_id=batch_id,
                    duration_ms=round((perf_counter() - started) * 1000),
                    candidates=candidates,
                    error_code=error_code,
                )
            )
        return model.name, tuple(results)
    finally:
        await engine.dispose()


def _write_report(
    path: Path,
    *,
    fixture: Path,
    model_name: str,
    report: MemoryEvaluationReport,
) -> None:
    try:
        fixture_label = str(
            fixture.resolve().relative_to(_BACKEND_ROOT.parent),
        )
    except ValueError:
        fixture_label = str(fixture)
    payload = {
        "checkpoint": "A",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "fixture": fixture_label,
        "model_name": model_name,
        "prompt_version": MEMORY_EXTRACT_PROMPT_VERSION,
        "extractor_version": MEMORY_EXTRACTOR_VERSION,
        "thresholds": {
            "precision": _PRECISION_THRESHOLD,
            "recall": _RECALL_THRESHOLD,
            "secret_leak_count": 0,
            "scope_violation_count": 0,
            "duplicate_candidate_count": 0,
            "batch_error_count": 0,
        },
        "metrics": asdict(report),
        "cost_observation": {
            "call_count": report.batch_count,
            "duration_ms": report.total_duration_ms,
            "token_usage": "unavailable_from_current_non_run_oneshot_api",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--model-ref")
    parser.add_argument(
        "--fixture-only",
        action="store_true",
        help="validate the fixed fixture without connecting to PostgreSQL or a model",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cases = load_eval_cases(args.fixture)
        if args.fixture_only:
            print(
                json.dumps(
                    {
                        "batch_count": len(_group_cases(cases)),
                        "sample_count": len(cases),
                        "status": "valid",
                    },
                    sort_keys=True,
                )
            )
            return 0
        model_name, batch_results = asyncio.run(
            _run_live_evaluation(cases, model_ref=args.model_ref),
        )
        report = score_eval_results(cases, batch_results)
        _write_report(
            args.output,
            fixture=args.fixture,
            model_name=model_name,
            report=report,
        )
    except (MemoryEvaluationInvalid, RuntimeError, ValueError):
        print("error: memory evaluation is unavailable", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "batch_count": report.batch_count,
                "passed": report.passed,
                "precision": report.precision,
                "recall": report.recall,
                "report": str(args.output),
                "sample_count": report.sample_count,
            },
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EvalBatchResult",
    "EvalCandidate",
    "ExpectedCandidate",
    "MemoryEvalCase",
    "MemoryEvaluationInvalid",
    "MemoryEvaluationReport",
    "load_eval_cases",
    "main",
    "score_eval_results",
]
