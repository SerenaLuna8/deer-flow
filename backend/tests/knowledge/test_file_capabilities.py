"""Capability reports are resource-derived and actual sandbox failures stay closed."""

from __future__ import annotations

from dataclasses import replace

import pytest
from actweave_knowledge.contracts import KnowledgeSettings
from actweave_knowledge.extraction.contracts import ExtractionError
from actweave_knowledge.extraction.registry import ExtractorRegistry, default_registry


def test_capabilities_report_registered_optional_dependencies_without_fallback():
    from actweave_knowledge.ingestion.profiles import build_file_capabilities

    registry = default_registry()
    registry = ExtractorRegistry(tuple(replace(r, dependency_probe=lambda: "PARSER_DEPENDENCY_UNAVAILABLE") if r.extractor_id == "unstructured.msg" else r for r in registry.registrations))
    capability = build_file_capabilities(KnowledgeSettings(etl_type="unstructured_local"), registry)
    msg = next(item for item in capability.formats if item.extension == ".msg")
    assert msg.available is False and msg.parser_id == "unstructured.msg"
    assert msg.reason_code == "PARSER_DEPENDENCY_UNAVAILABLE"
    assert next(item for item in capability.formats if item.extension == ".pdf").available is True
    assert capability.chunk_limits.parent_max_chars == 4000
    assert len(capability.capability_revision) == 64


@pytest.mark.asyncio
async def test_capability_probe_executes_actual_extraction_and_cleans_temp_directory(monkeypatch):
    from actweave_knowledge.extraction.runtime import run_extraction
    from actweave_knowledge.ingestion import profiles

    observed = []

    async def run(setting, **kwargs):
        observed.append(kwargs["work_dir"])
        assert setting.source_path.is_file()
        assert setting.source_path.read_text() == "Knowledge parser readiness."
        return await run_extraction(setting, **kwargs)

    monkeypatch.setattr(profiles, "run_extraction", run)
    capability = await profiles.probe_file_capabilities(KnowledgeSettings())
    assert observed and not observed[0].exists()
    assert all(item.available for item in capability.formats)


@pytest.mark.asyncio
async def test_binary_present_but_sandbox_denied_marks_every_format_unavailable(monkeypatch):
    from actweave_knowledge.ingestion import profiles

    async def denied(*args, **kwargs):
        raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE")

    monkeypatch.setattr(profiles, "run_extraction", denied)
    result = await profiles.probe_file_capabilities(KnowledgeSettings())
    assert result.formats and not any(item.available for item in result.formats)
    assert {item.reason_code for item in result.formats} == {"PARSER_SANDBOX_UNAVAILABLE"}
    assert not profiles.required_file_formats_ready(result)


@pytest.mark.asyncio
async def test_file_capabilities_http_uses_server_snapshot_not_frontend_format_list():
    from actweave_knowledge.ingestion.profiles import build_file_capabilities
    from test_upload import _PROJECT_ID, _app, _client, _FakeModule

    class Module(_FakeModule):
        async def file_capabilities(self, *, authority):
            assert authority.project_id == _PROJECT_ID
            return build_file_capabilities(KnowledgeSettings(), default_registry(), runtime_reason="PARSER_SANDBOX_UNAVAILABLE")

    async with _client(_app(Module())) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/file-capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["effective_etl"] == "builtin"
    assert not any(item["available"] for item in body["formats"])
    assert not {"minio", "storage_key", "source_path"} & body.keys()


@pytest.mark.asyncio
async def test_capabilities_revalidate_membership_on_every_read_despite_same_snapshot(postgres_database_url, tmp_path):
    from types import SimpleNamespace
    from uuid import UUID, uuid4

    from actweave_knowledge.contracts import KnowledgeError, KnowledgeMinioSettings
    from actweave_knowledge.module import KnowledgeModule
    from extraction_test_helpers import make_test_quota_port
    from sqlalchemy import text
    from test_upload import _harness, _seed_base, _upload

    from app.knowledge.authority import ProjectKnowledgeAuthority
    from app.knowledge.composition import (
        is_knowledge_project_active,
        is_knowledge_project_pending_deletion,
    )
    from app.projects.capabilities import Capability
    from app.projects.context import resolve_project_context

    harness = await _harness(postgres_database_url)
    module = None
    try:
        project, base = await _seed_base(harness)
        membership = uuid4()
        async with harness.factory() as session, session.begin():
            user = await session.scalar(text("SELECT created_by_user_id FROM projects WHERE id=:id"), {"id": project})
            await session.execute(text("INSERT INTO project_memberships(id,project_id,user_id,role,status,version) VALUES(:id,:project,:user,'editor','active',1)"), {"id": membership, "project": project, "user": user})
        async with harness.factory() as session:
            context = await resolve_project_context(session, UUID(user), project, "capability-test")
        authority = ProjectKnowledgeAuthority(context, Capability.SHARED_ASSETS_READ)
        settings = KnowledgeSettings(enabled=True, minio=KnowledgeMinioSettings(endpoint="localhost:9000", bucket="knowledge", access_key="test", secret_key="test"))
        module = KnowledgeModule(
            settings=settings,
            session_factory=harness.factory,
            model_port=SimpleNamespace(),
            quota=make_test_quota_port(harness.factory),
            project_active_check=is_knowledge_project_active,
            project_cleanup_check=is_knowledge_project_pending_deletion,
        )
        from actweave_knowledge.ingestion.profiles import build_file_capabilities

        module.install_file_capabilities(build_file_capabilities(settings, default_registry(), runtime_reason="PARSER_SANDBOX_UNAVAILABLE"))
        before = await module.file_capabilities(authority=authority)
        assert not any(item.available for item in before.formats)
        # Parser failure cannot disable existing project reads, but upload
        # must fail before the object-store adapter can receive any bytes.
        bases, total = await module.list_knowledge_bases(project, authority=authority)
        assert total == 1 and bases[0].id == base
        with pytest.raises(KnowledgeError) as parsing_error:
            await module.upload_document(project, base, _upload(tmp_path), authority=ProjectKnowledgeAuthority(context, Capability.SHARED_ASSETS_EDIT))
        assert parsing_error.value.reason_code == "PARSER_SANDBOX_UNAVAILABLE"
        async with harness.factory() as session, session.begin():
            await session.execute(text("UPDATE project_memberships SET status='removed',version=version+1,ended_at=now(),end_reason='removed' WHERE id=:id"), {"id": membership})
        with pytest.raises(KnowledgeError) as error:
            await module.file_capabilities(authority=authority)
        assert error.value.code == "KNOWLEDGE_NOT_FOUND"
    finally:
        if module:
            await module.aclose()
        await harness.engine.dispose()
