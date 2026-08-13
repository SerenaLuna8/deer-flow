from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.reliability.execution import PrivateRunJobHandler
from app.shared_assets.skill_design_generation import SkillBuilderDependencySnapshot


def _operation(**updates: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "status": "completed",
        "public_error_code": None,
        "result_revision": 3,
        "terminal_kind": "candidate",
        "terminal_request_checksum": "a" * 64,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _design(**updates: object) -> SimpleNamespace:
    draft_checksum = "b" * 64
    values: dict[str, object] = {
        "revision": 3,
        "thread_id": uuid.uuid4(),
        "status": "draft_ready",
        "draft_checksum": draft_checksum,
        "active_clarification_json": None,
        "validation_json": None,
        "validated_draft_checksum": None,
        "authoring_dependencies_json": SkillBuilderDependencySnapshot(
            draft_checksum=draft_checksum,
        ).model_dump(mode="json"),
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_completed_candidate_and_clarification_are_recoverable() -> None:
    candidate = _design()
    recovered_candidate = PrivateRunJobHandler._completed_skill_builder_terminal(
        _operation(),
        candidate,  # type: ignore[arg-type]
        thread_id=str(candidate.thread_id),
    )
    clarification = _design(
        status="awaiting_clarification",
        draft_checksum=None,
        authoring_dependencies_json=None,
        active_clarification_json={
            "version": 1,
            "kind": "human_input_request",
            "source": "skill-builder",
            "request_id": "clarification-1",
            "clarification_type": "skill_design",
            "title": "More information",
            "question": "Which format?",
            "context": "The output differs.",
            "input_mode": "free_text",
            "options": [],
        },
    )
    recovered_clarification = PrivateRunJobHandler._completed_skill_builder_terminal(
        _operation(terminal_kind="clarification"),
        clarification,  # type: ignore[arg-type]
        thread_id=str(clarification.thread_id),
    )

    assert recovered_candidate is not None
    assert recovered_candidate.status == "succeeded"
    assert recovered_clarification is not None
    assert recovered_clarification.status == "succeeded"


@pytest.mark.parametrize(
    ("operation_updates", "design_updates"),
    [
        ({"status": "failed", "public_error_code": "GENERATION_FAILED"}, {}),
        ({"terminal_kind": None, "terminal_request_checksum": None}, {}),
        ({"terminal_request_checksum": "not-a-checksum"}, {}),
        ({"result_revision": 2}, {}),
        ({}, {"status": "generating"}),
        ({}, {"draft_checksum": "c" * 64}),
        ({}, {"authoring_dependencies_json": None}),
        ({"terminal_kind": "clarification"}, {}),
    ],
)
def test_failed_legacy_or_mismatched_terminal_is_never_recovered(
    operation_updates: dict[str, object],
    design_updates: dict[str, object],
) -> None:
    design = _design(**design_updates)

    assert (
        PrivateRunJobHandler._completed_skill_builder_terminal(
            _operation(**operation_updates),
            design,  # type: ignore[arg-type]
            thread_id=str(design.thread_id),
        )
        is None
    )
