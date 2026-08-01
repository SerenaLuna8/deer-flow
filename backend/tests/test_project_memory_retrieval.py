from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from deerflow.agents.memory.manager import (
    PROJECT_MEMORY_CAPABILITIES,
    ProjectMemoryManager,
)
from deerflow.agents.memory.retrieval import rank_project_memory_facts

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _fact(
    fact_id: str,
    content: str,
    *,
    category: str = "context",
    confidence: object = 0.8,
    age_days: int = 0,
) -> dict[str, object]:
    return {
        "id": fact_id,
        "content": content,
        "category": category,
        "confidence": confidence,
        "createdAt": (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z"),
    }


def test_project_memory_capabilities_are_search_only_and_keep_passive_writes() -> None:
    assert PROJECT_MEMORY_CAPABILITIES.supports_search is True
    assert PROJECT_MEMORY_CAPABILITIES.supports_fact_mutation is False
    assert PROJECT_MEMORY_CAPABILITIES.requires_passive_writes is True


def test_ranker_handles_chinese_and_hyphenated_terms() -> None:
    results = rank_project_memory_facts(
        [
            _fact("cn", "用户要求数据库全程只使用 postgres 用户"),
            _fact("hyphen", "The project uses a LangGraph-based worker runtime"),
            _fact("other", "Unrelated preference"),
        ],
        "数据库 postgres",
        top_k=5,
        now=NOW,
    )
    assert [result["id"] for result in results] == ["cn"]

    results = rank_project_memory_facts(
        [
            _fact("cn", "用户要求数据库全程只使用 postgres 用户"),
            _fact("hyphen", "The project uses a LangGraph-based worker runtime"),
        ],
        "LangGraph-based",
        top_k=5,
        now=NOW,
    )
    assert [result["id"] for result in results] == ["hyphen"]


def test_category_filter_is_applied_before_top_k() -> None:
    results = rank_project_memory_facts(
        [
            _fact("high-context", "Python runtime", category="context", confidence=1.0),
            _fact("preference", "User prefers Python", category="preference", confidence=0.4),
        ],
        "Python",
        category="preference",
        top_k=1,
        now=NOW,
    )
    assert [result["id"] for result in results] == ["preference"]


def test_confidence_and_age_are_only_ranking_signals_after_a_text_match() -> None:
    results = rank_project_memory_facts(
        [
            _fact("fresh", "User prefers PostgreSQL", confidence=0.7, age_days=2),
            _fact("old", "User prefers PostgreSQL", confidence=0.99, age_days=400),
            _fact("unmatched", "Completely unrelated", confidence=1.0),
        ],
        "PostgreSQL",
        top_k=5,
        now=NOW,
    )
    assert [result["id"] for result in results] == ["fresh", "old"]
    assert results[0]["score"] > results[1]["score"]


@pytest.mark.parametrize("confidence", [None, float("nan"), float("inf"), "bad"])
def test_ranker_tolerates_invalid_confidence(confidence: object) -> None:
    results = rank_project_memory_facts(
        [_fact("fact", "PostgreSQL is authoritative", confidence=confidence)],
        "PostgreSQL",
        top_k=5,
        now=NOW,
    )
    assert results[0]["confidence"] == 0.5


def test_ranker_omits_malformed_facts_and_private_source_metadata() -> None:
    valid = _fact("safe-id", "Remember the exact project boundary")
    valid.update(
        {
            "source": "secret-thread",
            "sourceThreadId": "thread-secret",
            "sourceRunId": "run-secret",
            "project_id": "project-secret",
            "owner_user_id": "owner-secret",
        }
    )
    results = rank_project_memory_facts(
        [
            valid,
            {"content": "missing id"},
            {"id": "missing-content"},
            "not-a-dict",
        ],
        "project boundary",
        top_k=5,
        now=NOW,
    )
    assert len(results) == 1
    assert set(results[0]) == {
        "id",
        "content",
        "category",
        "confidence",
        "createdAt",
        "score",
        "matchType",
    }
    assert "secret" not in repr(results[0])


@pytest.mark.parametrize(
    ("query", "category", "top_k"),
    [
        ("", None, 5),
        ("   ", None, 5),
        ("x" * 1001, None, 5),
        ("valid", "x" * 33, 5),
        ("valid", None, 0),
        ("valid", None, 21),
        ("valid", None, True),
    ],
)
def test_ranker_rejects_unbounded_inputs(
    query: str,
    category: str | None,
    top_k: object,
) -> None:
    with pytest.raises(ValueError):
        rank_project_memory_facts(
            [_fact("fact", "valid")],
            query,
            category=category,
            top_k=top_k,  # type: ignore[arg-type]
            now=NOW,
        )


class _Authority:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def load_snapshot(self) -> object:
        self.calls += 1
        return self.snapshot


@pytest.mark.asyncio
async def test_manager_ranks_only_the_authorized_snapshot_and_binds_version() -> None:
    authority = _Authority(
        SimpleNamespace(
            version=7,
            facts=(
                SimpleNamespace(
                    id="fact-id",
                    content="User prefers concise answers",
                    category="preference",
                    confidence=0.9,
                    created_at=NOW,
                ),
            ),
            namespace="default",
            project_id="must-not-leak",
            owner_user_id="must-not-leak",
        )
    )

    response = await ProjectMemoryManager().asearch(
        authority=authority,
        query="concise",
        category=None,
        top_k=5,
        now=NOW,
    )

    assert authority.calls == 1
    assert response.snapshot_version == 7
    assert [result["id"] for result in response.results] == ["fact-id"]
    assert "must-not-leak" not in repr(response)


@pytest.mark.asyncio
async def test_manager_does_not_create_memory_when_authorized_snapshot_is_absent() -> None:
    authority = _Authority(None)
    response = await ProjectMemoryManager().asearch(
        authority=authority,
        query="anything",
        category=None,
        top_k=5,
        now=NOW,
    )
    assert response.snapshot_version is None
    assert response.results == ()
    assert authority.calls == 1
