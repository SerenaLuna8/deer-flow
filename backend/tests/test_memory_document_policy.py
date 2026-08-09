from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.gateway.routers.admin_system_settings import (
    _section_response,
    _update_response,
)
from app.system_runtime_settings.models import (
    MemoryDocumentPolicy,
    RuntimePolicySection,
    RuntimePolicyUpdateResult,
    RuntimePolicyView,
    default_policy_value,
)
from app.system_runtime_settings.validation import (
    RuntimePolicyInvalid,
    canonical_policy_payload,
)
from deerflow.config.app_config import AppConfig

LEGACY_MEMORY_DOCUMENT_SECTIONS = [
    "用户偏好与协作方式",
    "项目背景",
    "长期约束与架构决策",
    "当前仍有效的目标",
]


def test_memory_document_policy_defaults_are_canonical_plain_titles() -> None:
    assert RuntimePolicySection.MEMORY_DOCUMENT.value == "memory_document"
    policy = default_policy_value(RuntimePolicySection.MEMORY_DOCUMENT)

    assert isinstance(policy, MemoryDocumentPolicy)
    assert policy.sections == LEGACY_MEMORY_DOCUMENT_SECTIONS
    assert canonical_policy_payload(
        RuntimePolicySection.MEMORY_DOCUMENT,
        policy,
    ).value == {"sections": LEGACY_MEMORY_DOCUMENT_SECTIONS}


def test_memory_document_policy_trims_and_preserves_order() -> None:
    policy = MemoryDocumentPolicy(
        sections=["\u3000Personal context\u3000", "  Architecture decisions  "],
    )

    assert policy.sections == ["Personal context", "Architecture decisions"]


@pytest.mark.parametrize(
    "sections",
    [
        ["Only one"],
        [str(index) for index in range(9)],
        ["Duplicate", " Duplicate "],
        ["# Markdown heading", "Second"],
        ["Contains\nnewline", "Second"],
        ["Trailing newline\n", "Second"],
        ["Contains\ttab", "Second"],
        ["Trailing tab\t", "Second"],
        ["Unicode\u2028separator", "Second"],
        ["Trailing separator\u2029", "Second"],
        ["Contains\x00control", "Second"],
        ["Durable [DURABLE] facts", "Second"],
        ["History [H:12]", "Second"],
        ["x" * 81, "Second"],
        ["   ", "Second"],
        ["First", 2],
    ],
)
def test_memory_document_policy_rejects_invalid_sections(
    sections: list[object],
) -> None:
    with pytest.raises((ValidationError, RuntimePolicyInvalid)):
        canonical_policy_payload(
            RuntimePolicySection.MEMORY_DOCUMENT,
            {"sections": sections},
        )


def test_memory_document_policy_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryDocumentPolicy(
            sections=["First", "Second"],
            unsupported=True,  # type: ignore[call-arg]
        )


def test_example_config_has_no_memory_document_yaml_authority() -> None:
    payload = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "config.example.yaml").read_text(
            encoding="utf-8",
        )
    )

    assert "memory_document" not in payload


def test_yaml_memory_document_config_is_a_removed_legacy_source() -> None:
    with pytest.raises(ValidationError, match="LEGACY_CONFIG_REMOVED: memory_document"):
        AppConfig.model_validate(
            {"memory_document": {"sections": ["First", "Second"]}},
            context={"config_source": "yaml"},
        )


def test_admin_system_settings_maps_memory_document_update_strictly() -> None:
    now = datetime.now(UTC)
    view = RuntimePolicyView(
        section=RuntimePolicySection.MEMORY_DOCUMENT,
        revision=2,
        schema_version=2,
        value=MemoryDocumentPolicy(sections=["Personal context", "Decisions"]),
        effect_scope="new_memory_documents",
        effective_revision=2,
        updated_at=now,
    )

    assert _section_response(view).model_dump(mode="json") == {
        "revision": 2,
        "schema_version": 2,
        "value": {"sections": ["Personal context", "Decisions"]},
        "section": "memory_document",
        "effect_scope": "new_memory_documents",
        "effective_revision": 2,
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    response = _update_response(
        RuntimePolicyUpdateResult(
            catalog_revision=3,
            policy=view,
            effective_at=now,
        )
    )
    assert response.section == "memory_document"
    assert response.effect_scope == "new_memory_documents"
    assert response.policy.value == {
        "sections": ["Personal context", "Decisions"],
    }
