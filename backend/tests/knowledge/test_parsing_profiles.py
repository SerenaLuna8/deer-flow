"""Server-owned parser identity and source-bound preview admission."""

from __future__ import annotations

import pytest
from actweave_knowledge.contracts import KnowledgeError, KnowledgeSettings
from actweave_knowledge.extraction.contracts import ExtractionError, ProcessingProfile
from actweave_knowledge.extraction.registry import default_registry
from parsing_test_helpers import make_chunk_profile, make_parse_profile
from pydantic import ValidationError


def test_preview_identity_binds_source_bytes_and_both_profiles():
    from actweave_knowledge.ingestion.profiles import preview_fingerprint

    profile = ProcessingProfile(parse=make_parse_profile(".pdf"), chunk=make_chunk_profile())
    args = dict(extension=".pdf", profile=profile, capability_revision="r1")
    first = preview_fingerprint(source_sha256="a" * 64, **args)
    assert first != preview_fingerprint(source_sha256="b" * 64, **args)
    changed = profile.model_copy(update={"chunk": profile.chunk.model_copy(update={"size": 800})})
    assert first != preview_fingerprint(source_sha256="a" * 64, extension=".pdf", profile=changed, capability_revision="r1")
    assert first == preview_fingerprint(source_sha256="a" * 64, extension=".PDF", profile=profile, capability_revision="r1")
    assert first != preview_fingerprint(source_sha256="a" * 64, extension=".pdf", profile=profile, capability_revision="r2")


def test_server_resolves_etl_and_token_identity_with_user_header_rules():
    from actweave_knowledge.ingestion.profiles import ProcessingParameters, resolve_processing_profile

    parameters = ProcessingParameters(mode="parent_child", size=800, child_size=300, header_rules=({"sheet": None, "mode": "none"},))
    profile = resolve_processing_profile(KnowledgeSettings(etl_type="unstructured_local"), parameters, default_registry(), extension=".CSV")
    assert profile.parse.etl_type == "unstructured_local"
    assert profile.parse.extractor_id == "builtin.csv"
    assert profile.parse.header_rules[0].mode == "none"
    assert (profile.chunk.size, profile.chunk.child_size, profile.chunk.unit) == (800, 300, "token")
    assert profile.chunk.tokenizer_profile_id == "knowledge-cl100k-v1"
    assert len(profile.chunk.tokenizer_digest) == 64


@pytest.mark.parametrize(
    "bad",
    [
        {"extractor_version": "attacker"},
        {"etl_type": "builtin"},
        {"tokenizer_digest": "a" * 64},
        {"size": True},
        {"size": 199},
        {"overlap": 1000},
        {"mode": "parent_child", "child_size": 1000},
        {"separator": ""},
        {"header_rules": [{"sheet": None, "mode": "none"}, {"sheet": None, "mode": "auto"}]},
    ],
)
def test_user_parameters_reject_authority_and_invalid_limits(bad):
    from actweave_knowledge.ingestion.profiles import ProcessingParameters

    with pytest.raises(ValidationError):
        ProcessingParameters(**bad)


def test_reparse_http_body_rejects_unknown_identity_and_conflicting_parameters():
    from app.knowledge.gateway import KnowledgeDocumentReparseRequest

    assert KnowledgeDocumentReparseRequest(expected_version=1, processing_profile={"size": 800}).processing_profile.size == 800
    with pytest.raises(ValidationError):
        KnowledgeDocumentReparseRequest(expected_version=1, processing_profile={"extractor_id": "arbitrary"})
    with pytest.raises(ValidationError):
        KnowledgeDocumentReparseRequest(expected_version=1, chunk_size=900, processing_profile={"size": 800})


@pytest.mark.asyncio
async def test_upload_http_passes_strict_profile_and_fingerprint_and_rejects_conflicts():
    import json
    from uuid import uuid4

    from test_upload import _PROJECT_ID, _app, _client, _FakeModule

    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{uuid4()}/documents", files={"file": ("a.txt", b"abc", "text/plain")}, data={"processing_profile": json.dumps({"size": 800}), "expected_preview_fingerprint": "a" * 64}
        )
        assert response.status_code == 200
        upload = module.calls[-1][1][2]
        assert upload.processing_profile.size == 800
        assert upload.expected_preview_fingerprint == "a" * 64
        preview = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/chunk-preview",
            files={"file": ("a.txt", b"abc", "text/plain")},
            data={"processing_profile": json.dumps({"size": 800}), "expected_preview_fingerprint": "a" * 64},
        )
        assert preview.status_code == 200
        preview_request = module.calls[-1][1]
        assert preview_request.processing_profile.size == 800
        assert preview_request.expected_preview_fingerprint == "a" * 64
        response = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{uuid4()}/documents", files={"file": ("a.txt", b"abc", "text/plain")}, data={"chunk_size": "900", "processing_profile": json.dumps({"size": 800})})
        assert response.status_code == 422
        preview_conflict = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/chunk-preview",
            files={"file": ("a.txt", b"abc", "text/plain")},
            data={"chunk_size": "900", "processing_profile": json.dumps({"size": 800})},
        )
        assert preview_conflict.status_code == 422
        assert len(module.calls) == 2


def test_document_projection_labels_history_as_character_and_exposes_new_token_profile():
    from dataclasses import replace

    from test_upload import _document_view

    from app.knowledge.gateway import _document_response

    old = _document_response(_document_view())
    assert old.chunk_size_unit == "character" and old.tokenizer_profile_id is None
    profile = ProcessingProfile(parse=make_parse_profile(".txt"), chunk=make_chunk_profile())
    new = _document_response(replace(_document_view(), parsing_profile=profile))
    assert new.chunk_size_unit == "token" and new.tokenizer_profile_id == "knowledge-cl100k-v1"
    assert new.parsing_profile == profile


def test_frozen_profile_uses_original_etl_and_refuses_unknown_runtime_versions():
    from actweave_knowledge.ingestion.profiles import validate_frozen_processing_profile

    profile = ProcessingProfile(parse=make_parse_profile(".md"), chunk=make_chunk_profile())
    assert validate_frozen_processing_profile(profile.model_dump(mode="json"), extension=".md", registry=default_registry()) == profile
    value = profile.model_dump(mode="json")
    value["parse"]["extractor_version"] = "old-unavailable-build"
    with pytest.raises(ExtractionError) as error:
        validate_frozen_processing_profile(value, extension=".md", registry=default_registry())
    assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"


def test_task_reparse_projection_rejects_disagreement_with_frozen_profile():
    from actweave_knowledge.ingestion.profiles import chunk_settings
    from actweave_knowledge.persistence.tasks import validated_reparse_settings

    profile = ProcessingProfile(parse=make_parse_profile(".txt"), chunk=make_chunk_profile())
    frozen = {**chunk_settings(profile), "processing_profile": profile.model_dump(mode="json"), "capability_revision": "a" * 64}
    assert validated_reparse_settings(frozen) == frozen
    with pytest.raises(ExtractionError):
        validated_reparse_settings({**frozen, "chunk_size": 800})
    with pytest.raises(ExtractionError):
        validated_reparse_settings({**frozen, "project_id": "forged"})
    with pytest.raises(ExtractionError):
        validated_reparse_settings({"chunk_size": 1000})


def test_shared_multipart_policy_parses_profile_conflicts_and_fingerprint_once():
    from app.knowledge.gateway import multipart_processing_options

    parameters, fingerprint = multipart_processing_options(raw_profile='{"size":800}', expected_fingerprint="a" * 64, form_keys={"processing_profile", "chunk_overlap"}, legacy_values={"chunk_size": 1000, "chunk_overlap": 80})
    assert parameters.size == 800 and parameters.overlap == 80 and fingerprint == "a" * 64
    with pytest.raises(KnowledgeError):
        multipart_processing_options(raw_profile='{"size":800}', expected_fingerprint=None, form_keys={"processing_profile", "chunk_size"}, legacy_values={"chunk_size": 900})
    with pytest.raises(KnowledgeError):
        multipart_processing_options(raw_profile=None, expected_fingerprint="bad", form_keys=set(), legacy_values={})
