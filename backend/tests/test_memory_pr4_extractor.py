from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import deerflow.agents.memory.extractor as extractor_module
from deerflow.agents.memory.extractor import (
    ExtractedMemoryCandidate,
    MemoryCandidateExtractor,
    MemoryExtractionInvalid,
    MemoryExtractionSource,
    MemoryExtractionUnsafe,
    RunOneshotMemoryExtractionModelCaller,
)


class _ModelCaller:
    def __init__(
        self,
        content: str,
    ) -> None:
        self._response = content
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self,
        *,
        system_instruction: str,
        user_content: str,
    ) -> str:
        self.calls.append((system_instruction, user_content))
        return self._response


@pytest.mark.asyncio
async def test_default_model_caller_disables_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def run_oneshot_llm(**kwargs):
        calls.append(kwargs)
        return '{"candidates":[]}'

    monkeypatch.setattr(extractor_module, "run_oneshot_llm", run_oneshot_llm)
    caller = RunOneshotMemoryExtractionModelCaller(
        app_config=SimpleNamespace(),
        model_name="memory-test",
    )

    response = await caller(
        system_instruction="fixed",
        user_content='{"items":[]}',
    )

    assert calls[0]["model_name"] == "memory-test"
    assert calls[0]["attach_tracing"] is False
    assert calls[0]["run_name"] == "memory_extract"
    assert response == '{"candidates":[]}'


@pytest.mark.asyncio
async def test_extractor_uses_only_ordered_source_items_and_returns_strict_candidates() -> None:
    caller = _ModelCaller(
        json.dumps(
            {
                "candidates": [
                    {
                        "source_ordinal": 1,
                        "candidate_type": "preference",
                        "content": "用户偏好简洁回答。",
                        "confidence": 0.98,
                        "retention_class": "durable",
                        "sensitivity": "normal",
                    },
                    {
                        "source_ordinal": 0,
                        "candidate_type": "constraint",
                        "content": "项目统一使用 PostgreSQL。",
                        "confidence": 1.0,
                        "retention_class": "permanent",
                        "sensitivity": "normal",
                    },
                ]
            },
            ensure_ascii=False,
        ),
    )
    extractor = MemoryCandidateExtractor(caller)

    result = await extractor.extract(
        (
            MemoryExtractionSource(ordinal=0, content="这个项目统一使用 PostgreSQL。"),
            MemoryExtractionSource(ordinal=1, content="我偏好简洁回答。"),
        )
    )

    assert result.candidates == (
        ExtractedMemoryCandidate(
            source_ordinal=0,
            candidate_type="constraint",
            content="项目统一使用 PostgreSQL。",
            confidence=1.0,
            retention_class="permanent",
            sensitivity="normal",
        ),
        ExtractedMemoryCandidate(
            source_ordinal=1,
            candidate_type="preference",
            content="用户偏好简洁回答。",
            confidence=0.98,
            retention_class="durable",
            sensitivity="normal",
        ),
    )
    assert len(caller.calls) == 1
    system_instruction, user_content = caller.calls[0]
    assert "<current_memory>" not in system_instruction
    assert "Thread Summary:" not in system_instruction
    assert "tool" not in user_content.lower()
    assert json.loads(user_content) == {
        "items": [
            {"content": "这个项目统一使用 PostgreSQL。", "ordinal": 0},
            {"content": "我偏好简洁回答。", "ordinal": 1},
        ]
    }


@pytest.mark.asyncio
async def test_extractor_accepts_empty_result() -> None:
    result = await MemoryCandidateExtractor(
        _ModelCaller('{"candidates":[]}'),
    ).extract((MemoryExtractionSource(ordinal=0, content="帮我看看代码。"),))

    assert result.candidates == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "candidates": [],
            "unexpected": True,
        },
        {
            "candidates": [
                {
                    "source_ordinal": 2,
                    "candidate_type": "context",
                    "content": "不存在的来源。",
                    "confidence": 0.9,
                    "retention_class": "durable",
                    "sensitivity": "normal",
                }
            ]
        },
        {
            "candidates": [
                {
                    "source_ordinal": 0,
                    "candidate_type": "temporary_task",
                    "content": "临时任务。",
                    "confidence": 0.9,
                    "retention_class": "durable",
                    "sensitivity": "normal",
                }
            ]
        },
        {
            "candidates": [
                {
                    "source_ordinal": 0,
                    "candidate_type": "context",
                    "content": "内容。",
                    "confidence": 1.1,
                    "retention_class": "durable",
                    "sensitivity": "normal",
                }
            ]
        },
    ],
)
async def test_extractor_rejects_invalid_or_untraceable_output(
    payload: dict[str, object],
) -> None:
    extractor = MemoryCandidateExtractor(
        _ModelCaller(json.dumps(payload, ensure_ascii=False)),
    )

    with pytest.raises(MemoryExtractionInvalid) as error:
        await extractor.extract(
            (MemoryExtractionSource(ordinal=0, content="来源。"),),
        )

    assert error.value.code == "MEMORY_EXTRACT_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_extractor_rejects_secret_like_model_output() -> None:
    extractor = MemoryCandidateExtractor(
        _ModelCaller(
            json.dumps(
                {
                    "candidates": [
                        {
                            "source_ordinal": 0,
                            "candidate_type": "context",
                            "content": "API key: sk-proj-1234567890abcdef",
                            "confidence": 0.99,
                            "retention_class": "durable",
                            "sensitivity": "restricted",
                        }
                    ]
                }
            )
        )
    )

    with pytest.raises(MemoryExtractionUnsafe) as error:
        await extractor.extract(
            (MemoryExtractionSource(ordinal=0, content="普通来源。"),),
        )

    assert error.value.code == "MEMORY_EXTRACT_UNSAFE_OUTPUT"


@pytest.mark.asyncio
async def test_extractor_deduplicates_identical_candidates_but_rejects_conflicts() -> None:
    candidate = {
        "source_ordinal": 0,
        "candidate_type": "goal",
        "content": "用户希望完成记忆重构。",
        "confidence": 0.95,
        "retention_class": "durable",
        "sensitivity": "normal",
    }
    sources = (MemoryExtractionSource(ordinal=0, content="我要完成记忆重构。"),)
    result = await MemoryCandidateExtractor(
        _ModelCaller(json.dumps({"candidates": [candidate, candidate]})),
    ).extract(sources)
    assert len(result.candidates) == 1

    conflict = {**candidate, "candidate_type": "context"}
    with pytest.raises(MemoryExtractionInvalid):
        await MemoryCandidateExtractor(
            _ModelCaller(json.dumps({"candidates": [candidate, conflict]})),
        ).extract(sources)
