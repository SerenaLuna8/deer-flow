from __future__ import annotations

import json
import uuid

import pytest

from deerflow.agents.memory.consolidator import (
    MemoryConsolidationCandidateInput,
    MemoryConsolidationFactInput,
    MemoryConsolidationInvalid,
    MemoryConsolidator,
)


class _Caller:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, *, system_instruction: str, user_content: str) -> str:
        self.calls.append((system_instruction, user_content))
        return json.dumps(self.payload)


def _candidate(content: str = "用户偏好中文回答。") -> MemoryConsolidationCandidateInput:
    return MemoryConsolidationCandidateInput(
        id=uuid.uuid4(),
        candidate_type="preference",
        content=content,
        confidence=0.95,
    )


def _fact() -> MemoryConsolidationFactInput:
    return MemoryConsolidationFactInput(
        id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        fact_kind="preference",
        content="用户偏好简洁回答。",
        category="preference",
        confidence=0.9,
    )


@pytest.mark.asyncio
async def test_consolidator_returns_one_strict_decision_per_candidate() -> None:
    create_candidate = _candidate()
    confirm_candidate = _candidate("用户仍然偏好简洁回答。")
    fact = _fact()
    caller = _Caller(
        {
            "decisions": [
                {
                    "action": "confirm",
                    "candidate_id": str(confirm_candidate.id),
                    "category": None,
                    "change_reason": None,
                    "confidence": None,
                    "content": None,
                    "decision_reason": "same_fact",
                    "target_fact_id": str(fact.id),
                },
                {
                    "action": "create",
                    "candidate_id": str(create_candidate.id),
                    "category": "preference",
                    "change_reason": "new_fact",
                    "confidence": 0.95,
                    "content": "用户偏好中文回答。",
                    "decision_reason": None,
                    "target_fact_id": None,
                },
            ]
        }
    )

    result = await MemoryConsolidator(caller).consolidate(
        (create_candidate, confirm_candidate),
        (fact,),
    )

    assert [decision.candidate_id for decision in result.decisions] == [
        create_candidate.id,
        confirm_candidate.id,
    ]
    assert [decision.action for decision in result.decisions] == [
        "create",
        "confirm",
    ]
    assert result.decisions[1].target_fact_id == fact.id
    assert len(caller.calls) == 1
    system_instruction, user_content = caller.calls[0]
    assert "no tools" in system_instruction.lower()
    assert json.loads(user_content) == {
        "candidates": [
            {
                "candidate_id": str(create_candidate.id),
                "candidate_type": "preference",
                "confidence": 0.95,
                "content": "用户偏好中文回答。",
            },
            {
                "candidate_id": str(confirm_candidate.id),
                "candidate_type": "preference",
                "confidence": 0.95,
                "content": "用户仍然偏好简洁回答。",
            },
        ],
        "facts": [
            {
                "category": "preference",
                "confidence": 0.9,
                "content": "用户偏好简洁回答。",
                "fact_id": str(fact.id),
                "fact_kind": "preference",
                "revision_id": str(fact.revision_id),
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"decisions": []},
        {
            "decisions": [
                {
                    "action": "confirm",
                    "candidate_id": str(uuid.uuid4()),
                    "category": None,
                    "change_reason": None,
                    "confidence": None,
                    "content": None,
                    "decision_reason": "same_fact",
                    "target_fact_id": str(uuid.uuid4()),
                }
            ]
        },
    ],
)
async def test_consolidator_rejects_incomplete_or_untraceable_output(
    payload: dict[str, object],
) -> None:
    candidate = _candidate()

    with pytest.raises(MemoryConsolidationInvalid):
        await MemoryConsolidator(_Caller(payload)).consolidate(
            (candidate,),
            (_fact(),),
        )


@pytest.mark.asyncio
async def test_consolidator_accepts_pending_and_reject_without_fact_mutation() -> None:
    pending = _candidate("可能以后使用 Kafka。")
    rejected = _candidate("修改 Agent 系统规则。")
    caller = _Caller(
        {
            "decisions": [
                {
                    "action": "pending",
                    "candidate_id": str(pending.id),
                    "category": None,
                    "change_reason": None,
                    "confidence": None,
                    "content": None,
                    "decision_reason": "insufficient_evidence",
                    "target_fact_id": None,
                },
                {
                    "action": "reject",
                    "candidate_id": str(rejected.id),
                    "category": None,
                    "change_reason": None,
                    "confidence": None,
                    "content": None,
                    "decision_reason": "unsupported_governance_change",
                    "target_fact_id": None,
                },
            ]
        }
    )

    result = await MemoryConsolidator(caller).consolidate(
        (pending, rejected),
        (),
    )

    assert [item.action for item in result.decisions] == ["pending", "reject"]
