"""Knowledge settings are revisioned, admin-only and never expose credentials."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.system_model_seed import seed_system_model_config

from app.audit.models import resolve_system_audit_context
from app.audit.service import AuditService
from app.knowledge_settings.bootstrap import bootstrap_knowledge_system_settings
from app.knowledge_settings.models import AdminKnowledgeSettingsUpdateRequest
from app.knowledge_settings.service import (
    KnowledgeSettingsError,
    knowledge_settings_response,
    load_knowledge_settings_from_db,
    read_active_summary_model,
    read_knowledge_system_settings,
    update_knowledge_system_settings,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.persistence.system_settings import SystemModelConfigRow
from deerflow.secrets import SecretKey

pytestmark = [pytest.mark.postgres, pytest.mark.anyio]


def _request(**changes):
    return AdminKnowledgeSettingsUpdateRequest(
        **{
            "expected_revision": 1,
            "enabled": False,
            "worker_concurrency": 2,
            "task_timeout_seconds": 900,
            "upload_max_bytes": 52428800,
            "max_knowledge_bases_per_project": 20,
            "max_documents_per_knowledge_base": 500,
            "max_segments_per_document": 5000,
            "minio_endpoint": None,
            "minio_bucket": None,
            "minio_access_key": None,
            "minio_secure": False,
            "summary_model_name": None,
            "query_cache_enabled": True,
            "query_cache_max_entries": 512,
            "query_cache_ttl_seconds": 300,
            **changes,
        }
    )


@pytest.fixture
async def settings_env(migrated_postgres_database_url, monkeypatch):
    monkeypatch.setenv("ACT_WEAVE_AUDIT_ACTIVE_KEY_ID", "test-audit-v1")
    monkeypatch.setenv("ACT_WEAVE_AUDIT_KEYRING_JSON", '{"test-audit-v1":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}')
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = uuid.uuid4()
    try:
        await bootstrap_knowledge_system_settings(factory)
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version) VALUES (:id,:email,'system_admin',now(),false,0)"),
                {"id": str(actor_id), "email": f"{actor_id}@example.invalid"},
            )
        actor = resolve_system_audit_context(SimpleNamespace(id=actor_id, system_role="system_admin"), request_id=str(uuid.uuid4()))
        yield SimpleNamespace(
            engine=engine,
            factory=factory,
            actor=actor,
            secret_key=SecretKey(b"k" * 32),
            audit_service=AuditService(factory, AuditHmacKeyring.from_environment()),
        )
    finally:
        await engine.dispose()


async def _save(env, request, probe=None):
    async def ready(_settings):
        return None

    return await update_knowledge_system_settings(
        env.factory,
        actor=env.actor,
        request=request,
        secret_key=env.secret_key,
        audit_service=env.audit_service,
        storage_probe=probe or ready,
    )


async def test_bootstrap_preserves_existing_revision_and_defaults(settings_env):
    env = settings_env
    async with env.factory() as session:
        before = await read_knowledge_system_settings(session)
        assert before.enabled is False and before.revision == 1
        assert before.minio_secret_ciphertext is None
    saved = await _save(env, _request(worker_concurrency=3))
    await bootstrap_knowledge_system_settings(env.factory)
    async with env.factory() as session:
        after = await read_knowledge_system_settings(session)
        assert (after.revision, after.worker_concurrency, after.updated_at) == (2, 3, saved.updated_at)


async def test_save_encrypts_secret_and_audits_only_empty_metadata(settings_env):
    env = settings_env
    secret = "only-in-request-and-memory"
    request = _request(enabled=True, minio_endpoint="localhost:9000", minio_bucket="knowledge", minio_access_key="operator", minio_secret_key=secret)
    assert secret not in repr(request)
    assert "minio_secret_key" not in request.model_dump()
    probe_calls = []

    async def probe(settings):
        # Storage I/O must be outside every DB transaction; a separate update
        # of the same singleton can acquire its lock without waiting.
        async with env.factory() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            row = await session.get(KnowledgeSystemSettingsRow, 1, with_for_update=True)
            assert row is not None and row.revision == 1
        probe_calls.append(settings.secret_key.get_secret_value())

    saved = await _save(env, request, probe)
    assert saved.revision == 2 and probe_calls == [secret]
    assert secret.encode() not in saved.minio_secret_ciphertext
    assert secret not in repr(saved)
    loaded = await load_knowledge_settings_from_db(env.factory, secret_key=env.secret_key)
    assert loaded.enabled and loaded.minio.secret_key.get_secret_value() == secret
    async with env.factory() as session:
        response = await knowledge_settings_response(session, saved, request_id="test")
        assert response.secret_key_configured
        assert "minio_secret_key" not in response.model_dump_json()
        assert secret not in response.model_dump_json()
        events = (await session.scalars(select(AuditLogRow).where(AuditLogRow.action == "knowledge_settings.update"))).all()
        assert len(events) == 1
        assert events[0].metadata_json == {}


async def test_probe_failure_and_cas_conflict_leave_settings_unchanged(settings_env):
    env = settings_env

    async def failed(_settings):
        raise RuntimeError("credential-bearing-provider-error")

    with pytest.raises(KnowledgeSettingsError) as rejected:
        await _save(env, _request(enabled=True, minio_endpoint="localhost:9000", minio_bucket="knowledge", minio_access_key="operator", minio_secret_key="test-secret"), failed)
    assert rejected.value.status_code == 422
    assert "credential" not in str(rejected.value)
    async with env.factory() as session:
        assert (await read_knowledge_system_settings(session)).revision == 1
    await _save(env, _request(worker_concurrency=4))
    with pytest.raises(KnowledgeSettingsError) as conflict:
        await _save(env, _request(worker_concurrency=5))
    assert conflict.value.status_code == 409
    async with env.factory() as session:
        assert (await read_knowledge_system_settings(session)).worker_concurrency == 4


async def test_cas_rechecked_after_probe(settings_env):
    env = settings_env

    async def concurrent_save(_settings):
        await _save(env, _request(worker_concurrency=7))

    with pytest.raises(KnowledgeSettingsError) as rejected:
        await _save(env, _request(enabled=True, minio_endpoint="localhost:9000", minio_bucket="knowledge", minio_access_key="operator", minio_secret_key="test-secret"), concurrent_save)
    assert rejected.value.status_code == 409
    async with env.factory() as session:
        row = await read_knowledge_system_settings(session)
        assert row.worker_concurrency == 7 and row.minio_secret_ciphertext is None


async def test_retained_secret_cannot_move_to_new_endpoint(settings_env):
    env = settings_env
    storage = dict(minio_endpoint="localhost:9000", minio_bucket="knowledge", minio_access_key="operator")
    await _save(env, _request(**storage, minio_secret_key="saved-secret"))
    await _save(env, _request(**storage, expected_revision=2, enabled=True))
    with pytest.raises(KnowledgeSettingsError) as rejected:
        await _save(env, _request(**{**storage, "minio_endpoint": "other:9000"}, expected_revision=3))
    assert rejected.value.status_code == 422
    with pytest.raises(ValidationError):
        _request(minio_secret_key="")
    with pytest.raises(ValidationError):
        _request(summary_model_name="not-a-uuid")
    with pytest.raises(ValidationError):
        _request(untrusted_authority="system_admin")


async def test_summary_model_must_be_live_and_can_be_cleared(settings_env):
    env = settings_env
    model_id = uuid.uuid4()
    async with env.factory() as session, session.begin():
        await seed_system_model_config(session, model_id=model_id, owner_user_id=str(env.actor.user_id), display_name="Summary model", provider_model="summary/test")
    saved = await _save(env, _request(summary_model_name=str(model_id)))
    async with env.factory() as session:
        info = await read_active_summary_model(session)
        assert info.model_name == str(model_id) and info.display_name == "Summary model"
    async with env.factory() as session, session.begin():
        model = await session.get(SystemModelConfigRow, model_id)
        model.status = "suspended"
    with pytest.raises(KnowledgeSettingsError) as rejected:
        await _save(env, _request(expected_revision=2, summary_model_name=str(model_id)))
    assert rejected.value.status_code == 422
    async with env.factory() as session:
        response = await knowledge_settings_response(session, saved, request_id="test")
        assert response.summary_model_name == str(model_id) and response.summary_model is None
    await _save(env, _request(expected_revision=2))


async def test_enabled_requires_storage_and_revoked_admin_cannot_save(settings_env):
    env = settings_env
    with pytest.raises(KnowledgeSettingsError) as invalid:
        await _save(env, _request(enabled=True))
    assert invalid.value.status_code == 422
    async with env.factory() as session, session.begin():
        await session.execute(text("UPDATE users SET system_role='user' WHERE id=:id"), {"id": str(env.actor.user_id)})
    with pytest.raises(KnowledgeSettingsError) as revoked:
        await _save(env, _request())
    assert revoked.value.status_code == 404


async def test_admin_api_redacts_secret_and_hides_nonadmins(settings_env, monkeypatch):
    import base64

    import httpx
    from fastapi import FastAPI

    from app.gateway.deps import get_current_user_from_request, get_project_audit_service, project_session
    from app.gateway.routers.admin_knowledge_settings import router
    from app.reliability.error_mapping import ReliabilityHTTPException, reliability_http_exception_handler

    env = settings_env
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", base64.b64encode(b"k" * 32).decode())
    app = FastAPI()
    app.add_exception_handler(ReliabilityHTTPException, reliability_http_exception_handler)
    app.include_router(router)
    app.state.admin_operations_session_factory = env.factory

    async def current_session():
        async with env.factory() as session:
            yield session

    app.dependency_overrides[project_session] = current_session

    async def current_user(request: Request):
        return SimpleNamespace(id=str(env.actor.user_id))

    app.dependency_overrides[get_current_user_from_request] = current_user
    app.dependency_overrides[get_project_audit_service] = lambda: env.audit_service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        initial = await client.get("/api/admin/settings/knowledge")
        assert initial.status_code == 200
        assert initial.json()["secret_key_configured"] is False
        payload = _request(minio_endpoint="storage.invalid:9000", minio_access_key="operator", minio_bucket="knowledge").model_dump(mode="json")
        payload["minio_secret_key"] = "never-echo-this-secret"
        saved = await client.put("/api/admin/settings/knowledge", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 2 and saved.json()["secret_key_configured"]
        assert "never-echo" not in saved.text and "minio_secret_key" not in saved.json()
        stale = await client.put("/api/admin/settings/knowledge", json=payload)
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "KNOWLEDGE_SETTINGS_CONFLICT"
        invalid = await client.put("/api/admin/settings/knowledge", json={**payload, "minio_secret_key": {"payload": "never-echo-this-secret"}})
        assert invalid.status_code in (400, 422) and "never-echo" not in invalid.text
        async with env.factory() as session, session.begin():
            await session.execute(text("UPDATE users SET system_role='user' WHERE id=:id"), {"id": str(env.actor.user_id)})
        assert (await client.get("/api/admin/settings/knowledge")).status_code == 404
        assert (await client.put("/api/admin/settings/knowledge", json=payload)).status_code == 404


async def test_explicit_setup_hook_seeds_default_singleton(settings_env):
    from scripts.setup_postgres import _bootstrap_knowledge_settings_schema

    env = settings_env
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM knowledge_system_settings"))
    await _bootstrap_knowledge_settings_schema(env.engine)
    async with env.factory() as session:
        row = await session.get(KnowledgeSystemSettingsRow, 1)
        assert row is not None and row.revision == 1 and row.enabled is False


async def test_legacy_config_migration_is_idempotent_encrypted_and_does_not_call_runtime_loader(settings_env, tmp_path, monkeypatch):
    from deerflow.config.app_config import AppConfig
    from scripts.migrate_knowledge_config import migrate_knowledge_config, migration_report

    env = settings_env
    config = tmp_path / "legacy.yaml"
    config.write_text("knowledge:\n  enabled: true\n  worker_concurrency: 4\n  minio:\n    endpoint: localhost:9000\n    bucket: migration-bucket\n    access_key: operator\n    secret_key: $M11_MIGRATION_TEST_SECRET\n", encoding="utf-8")
    monkeypatch.setenv("M11_MIGRATION_TEST_SECRET", "migration-private-secret")

    def runtime_must_not_run(*args, **kwargs):
        raise AssertionError("migration must not invoke the tombstoned runtime loader")

    monkeypatch.setattr(AppConfig, "from_file", runtime_must_not_run)
    first = await migrate_knowledge_config(env.factory, config_path=config, secret_key=env.secret_key)
    second = await migrate_knowledge_config(env.factory, config_path=config, secret_key=env.secret_key)
    assert second.revision == first.revision + 1
    loaded = await load_knowledge_settings_from_db(env.factory, secret_key=env.secret_key)
    assert loaded.worker_concurrency == 4 and loaded.minio.secret_key.get_secret_value() == "migration-private-secret"
    assert loaded.minio.bucket == "migration-bucket"
    assert "migration-private-secret" not in migration_report()
    assert "localhost:9000" not in migration_report()
    assert "[REDACTED]" in migration_report()


async def test_invalid_migration_leaves_settings_unchanged(settings_env, tmp_path):
    from scripts.migrate_knowledge_config import migrate_knowledge_config

    env = settings_env
    config = tmp_path / "invalid.yaml"
    config.write_text("knowledge:\n  enabled: true\n  minio:\n    secret_key: never-print-this\n", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        await migrate_knowledge_config(env.factory, config_path=config, secret_key=env.secret_key)
    assert "never-print-this" not in str(error.value)
    async with env.factory() as session:
        assert (await read_knowledge_system_settings(session)).revision == 1


async def test_storage_probe_returns_when_s3_peer_accepts_but_never_responds():
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from actweave_knowledge import KnowledgeError
    from actweave_knowledge.contracts import KnowledgeMinioSettings

    from app.knowledge_settings.service import probe_knowledge_storage

    release = threading.Event()

    class SilentS3Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            release.wait(20)
            try:
                self.send_error(503)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_HEAD = do_GET

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SilentS3Peer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    task = asyncio.create_task(probe_knowledge_storage(KnowledgeMinioSettings(endpoint=f"127.0.0.1:{server.server_port}", bucket="probe", access_key="test-access", secret_key="test-secret")))
    try:
        done, _pending = await asyncio.wait({task}, timeout=4)
        assert task in done, "settings probe retained the MinIO SDK's five-minute socket timeout"
        with pytest.raises(KnowledgeError):
            await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
