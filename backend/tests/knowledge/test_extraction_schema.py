"""Extraction ownership and atomic publication gates on fresh Schema V1."""

import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from actweave_knowledge.extraction.contracts import ProcessingProfile
from extraction_test_helpers import installed_knowledge_sessions, seed_scope
from parsing_test_helpers import make_chunk_profile, make_parse_profile
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from deerflow.persistence.final_schema_contract import FINAL_SCHEMA_V1_CATALOG_SIGNATURE, read_schema_v1_catalog_signature


@pytest.mark.asyncio
async def test_same_document_and_generation_constraints(postgres_database_url):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        text("""
                SELECT conname, pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conname IN (
                  'fk_knowledge_extractions_document',
                  'fk_knowledge_attachments_extraction',
                  'fk_knowledge_segment_attachments_segment',
                  'fk_knowledge_segment_attachments_attachment',
                  'fk_knowledge_documents_published_extraction',
                  'fk_knowledge_segments_published_extraction')
            """)
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 6
        constraints = {row["conname"]: row["definition"] for row in rows}
        assert "project_id, knowledge_base_id, knowledge_document_id" in constraints["fk_knowledge_attachments_extraction"]
        assert "DEFERRABLE INITIALLY DEFERRED" in constraints["fk_knowledge_segments_published_extraction"]


def scope_values(scope):
    return dict(zip(("project_id", "knowledge_base_id", "knowledge_document_id"), scope, strict=True))


async def insert_row(session, table, values):
    """Use real SQL facts; table/columns are test-owned constants, never input."""
    names = ", ".join(values)
    parameters = ", ".join(f":{name}" for name in values)
    await session.execute(text(f"INSERT INTO {table} ({names}) VALUES ({parameters})"), values)


async def extraction(session, scope, **overrides):

    extraction_id = uuid.uuid4()
    values = dict(
        id=extraction_id,
        **scope_values(scope),
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        normalization_version="normalize_v1",
        state="ready",
        manifest_storage_key=f"manifests/{extraction_id}",
        manifest_sha256="c" * 64,
        manifest_size_bytes=128,
        manifest_upload_state="stored",
        manifest_quota_state="committed",
        created_task_id=uuid.uuid4(),
        created_attempt=1,
        created_claim_token=uuid.uuid4(),
        target_document_version=1,
        completed_at=datetime.now(UTC),
        unpublished_expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    values.update(overrides)
    await insert_row(session, "knowledge_extractions", values)
    return extraction_id


async def attachment(session, scope, extraction_id, **overrides):

    attachment_id = uuid.uuid4()
    values = dict(
        id=attachment_id,
        extraction_id=extraction_id,
        **scope_values(scope),
        sha256="d" * 64,
        media_type="image/png",
        size_bytes=64,
        width=1,
        height=1,
        storage_key=f"images/{attachment_id}",
        state="ready",
        upload_state="stored",
        quota_state="committed",
    )
    values.update(overrides)
    await insert_row(session, "knowledge_attachments", values)
    return attachment_id


async def segment(session, scope, extraction_id, **overrides):

    segment_id = uuid.uuid4()
    values = dict(
        id=segment_id,
        **scope_values(scope),
        extraction_id=extraction_id,
        document_version=1,
        position=1,
        content="Before ![image](knowledge-attachment:" + "d" * 64 + ") after",
        index_text="Before image after",
        token_count=3,
        source_spans="[]",
    )
    values.update(overrides)
    await insert_row(session, "knowledge_segments", values)
    return segment_id


async def occurrence(session, scope, extraction_id, segment_id, attachment_id, **overrides):
    values = dict(**scope_values(scope), extraction_id=extraction_id, segment_id=segment_id, attachment_id=attachment_id, position=1, alt_text="image")
    values.update(overrides)
    await insert_row(session, "knowledge_segment_attachments", values)


async def publish(session, document_id, extraction_id):
    await session.execute(text("UPDATE knowledge_documents SET published_extraction_id=:eid WHERE id=:id"), {"eid": extraction_id, "id": document_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_column", ["project_id", "knowledge_base_id", "knowledge_document_id"])
async def test_ownership_rejects_scope_forgery(postgres_database_url, scope_column):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        other = await seed_scope(sessions)
        forged = scope_values(scope) | {scope_column: scope_values(other)[scope_column]}
        bad_scope = tuple(forged.values())
        async with sessions() as session, session.begin():
            eid = await extraction(session, scope)
            aid = await attachment(session, scope, eid)
            sid = await segment(session, scope, eid)
            await publish(session, scope[2], eid)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await extraction(session, bad_scope)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await attachment(session, bad_scope, eid)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await occurrence(session, bad_scope, eid, sid, aid)
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await publish(session, other[2], eid)


@pytest.mark.asyncio
async def test_generation_binding_preserves_duplicate_occurrences(postgres_database_url):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            eid = await extraction(session, scope)
            next_eid = await extraction(session, scope)
            aid = await attachment(session, scope, eid)
            other_aid = await attachment(session, scope, next_eid)
            sid = await segment(session, scope, eid)
            await publish(session, scope[2], eid)
            await occurrence(session, scope, eid, sid, aid)
            await occurrence(session, scope, eid, sid, aid, position=2)
            assert await session.scalar(text("SELECT count(*) FROM knowledge_segment_attachments")) == 2
            for generation, image in [(eid, other_aid), (next_eid, other_aid)]:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await occurrence(session, scope, generation, sid, image, position=3)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await attachment(session, scope, eid)  # Same extraction/hash, new storage key.
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await occurrence(session, scope, eid, sid, aid, position=1)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await occurrence(session, scope, eid, sid, aid, position=0)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await occurrence(session, scope, None, sid, aid, position=3)


@pytest.mark.asyncio
async def test_published_pointer_switch_requires_atomic_segment_replacement(postgres_database_url):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            eid = await extraction(session, scope)
            new_eid = await extraction(session, scope)
            aid = await attachment(session, scope, eid)
            new_aid = await attachment(session, scope, new_eid)
            sid = await segment(session, scope, eid)
            await occurrence(session, scope, eid, sid, aid)
            # The pointer is set after segments: both FKs defer to commit.
            await publish(session, scope[2], eid)
        for candidate in (new_eid, None):
            async with sessions() as session:
                await publish(session, scope[2], candidate)
                with pytest.raises(IntegrityError):
                    await session.commit()
        async with sessions() as session, session.begin():
            # Conversely pointer first, segment replacement second is atomic.
            await publish(session, scope[2], new_eid)
            await session.execute(text("DELETE FROM knowledge_segments WHERE id=:id"), {"id": sid})
            new_sid = await segment(session, scope, new_eid)
            await occurrence(session, scope, new_eid, new_sid, new_aid)
        async with sessions() as session:
            assert await session.scalar(text("SELECT published_extraction_id FROM knowledge_documents WHERE id=:id"), {"id": scope[2]}) == new_eid
            assert await session.scalar(text("SELECT segment_id FROM knowledge_segment_attachments")) == new_sid
            assert await session.scalar(text("SELECT count(*) FROM knowledge_attachments")) == 2


@pytest.mark.asyncio
async def test_byte_ownership_is_not_cascaded_and_task_pin_blocks_reclamation(postgres_database_url):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            task_id = uuid.uuid4()
            await insert_row(session, "knowledge_tasks", dict(id=task_id, project_id=scope[0], resource_id=scope[2], kind="ingest_document", target_version=1))
            eid = await extraction(session, scope, created_task_id=task_id)
            aid = await attachment(session, scope, eid)
            for table, row_id in [("knowledge_documents", scope[2]), ("knowledge_extractions", eid)]:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(text(f"DELETE FROM {table} WHERE id=:id"), {"id": row_id})
            await session.execute(text("DELETE FROM knowledge_attachments WHERE id=:id"), {"id": aid})
            await session.execute(text("UPDATE knowledge_tasks SET extraction_id=:eid WHERE id=:id"), {"id": task_id, "eid": eid})
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text("DELETE FROM knowledge_extractions WHERE id=:id"), {"id": eid})
            await session.execute(text("UPDATE knowledge_tasks SET status='succeeded',finished_at=now(),extraction_id=NULL WHERE id=:id"), {"id": task_id})
            await session.execute(text("DELETE FROM knowledge_tasks WHERE id=:id"), {"id": task_id})
            # Immutable creation evidence carries no FK to retained task history.
            assert await session.scalar(text("SELECT count(*) FROM knowledge_extractions")) == 1
            await session.execute(text("DELETE FROM knowledge_extractions WHERE id=:id"), {"id": eid})


@pytest.mark.asyncio
async def test_pin_closed_status_kind_and_project_guards(postgres_database_url):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope, other = await seed_scope(sessions), await seed_scope(sessions)
        async with sessions() as session, session.begin():
            eid = await extraction(session, scope)
            for status in ("queued", "running", "retry_wait"):
                for kind in ("ingest_document", "reembed_document", "summarize_document"):
                    values = dict(id=uuid.uuid4(), project_id=scope[0], resource_id=uuid.uuid4(), kind=kind, target_version=1, status=status, extraction_id=eid)
                    if status == "running":
                        values.update(claim_token=uuid.uuid4(), lease_until=datetime.now(UTC) + timedelta(minutes=1), attempt_count=1)
                    await insert_row(session, "knowledge_tasks", values)
            for invalid in [
                dict(status="succeeded", finished_at=datetime.now(UTC)),
                dict(status="failed", finished_at=datetime.now(UTC)),
                dict(kind="delete_extraction", target_version=None),
                dict(kind="delete_document", target_version=None),
                dict(project_id=other[0]),
            ]:
                values = dict(id=uuid.uuid4(), project_id=scope[0], resource_id=uuid.uuid4(), kind="ingest_document", target_version=1, extraction_id=eid)
                values.update(invalid)
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await insert_row(session, "knowledge_tasks", values)


@pytest.mark.asyncio
async def test_delete_extraction_task_has_one_open_slot_and_no_locator(postgres_database_url):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            eid = await extraction(session, scope)
            values = dict(id=uuid.uuid4(), project_id=scope[0], resource_id=eid, kind="delete_extraction")
            await insert_row(session, "knowledge_tasks", values)
            for invalid in [dict(id=uuid.uuid4()), dict(id=uuid.uuid4(), resource_id=uuid.uuid4(), storage_key="must-not-be-accepted"), dict(id=uuid.uuid4(), resource_id=uuid.uuid4(), target_version=1)]:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await insert_row(session, "knowledge_tasks", values | invalid)
            await session.execute(text("UPDATE knowledge_tasks SET status='succeeded',finished_at=now() WHERE id=:id"), {"id": values["id"]})
            await insert_row(session, "knowledge_tasks", values | dict(id=uuid.uuid4()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity,invalid",
    [
        ("extraction", dict(source_sha256="A" * 64)),
        ("extraction", dict(parser_fingerprint="z" * 64)),
        ("extraction", dict(manifest_sha256="a" * 63)),
        ("extraction", dict(manifest_storage_key=None)),
        ("extraction", dict(manifest_size_bytes=-1)),
        ("extraction", dict(manifest_size_bytes=52428801)),
        ("extraction", dict(manifest_upload_state="pending")),
        ("extraction", dict(manifest_quota_state="reserved")),
        ("extraction", dict(completed_at=None)),
        ("extraction", dict(state="invalid")),
        ("extraction", dict(manifest_upload_state="invalid")),
        ("extraction", dict(manifest_quota_state="invalid")),
        ("extraction", dict(state="deleting", manifest_quota_state="released")),
        ("attachment", dict(sha256="x" * 64)),
        ("attachment", dict(media_type="image/svg+xml")),
        ("attachment", dict(size_bytes=-1)),
        ("attachment", dict(size_bytes=5242881)),
        ("attachment", dict(width=0)),
        ("attachment", dict(width=20000001, height=1)),
        ("attachment", dict(width=2147483647, height=2147483647)),
        ("attachment", dict(upload_state="pending")),
        ("attachment", dict(quota_state="reserved")),
        ("attachment", dict(state="invalid")),
        ("attachment", dict(upload_state="invalid")),
        ("attachment", dict(quota_state="invalid")),
        ("attachment", dict(state="deleting", quota_state="released")),
    ],
)
async def test_object_facts_reject_invalid_states(postgres_database_url, entity, invalid):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            eid = await extraction(session, scope)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    if entity == "extraction":
                        await extraction(session, scope, **invalid)
                    else:
                        await attachment(session, scope, eid, **invalid)


@pytest.mark.asyncio
async def test_deletion_facts_can_wait_for_quota_release(postgres_database_url):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            eid = await extraction(session, scope, state="staging", manifest_storage_key=None, manifest_sha256=None, manifest_size_bytes=0, manifest_upload_state="pending", manifest_quota_state="unreserved", completed_at=None)
            for fact in ("reserved", "committed", "released"):
                deleted_eid = await extraction(session, scope, state="deleting", manifest_upload_state="deleted", manifest_quota_state=fact)
                await attachment(session, scope, deleted_eid, state="deleting", upload_state="deleted", quota_state=fact)
                await session.execute(text("UPDATE knowledge_documents SET upload_state='deleted', quota_state=:fact WHERE id=:id"), {"id": scope[2], "fact": fact})
            facts = (await session.execute(text("SELECT manifest_storage_key,manifest_sha256,manifest_size_bytes,manifest_quota_state,manifest_upload_state FROM knowledge_extractions WHERE id=:id"), {"id": eid})).one()
            assert facts == (None, None, 0, "unreserved", "pending")


@pytest.mark.asyncio
async def test_profile_json_index_defaults_and_settings(postgres_database_url):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            profile = ProcessingProfile(parse=make_parse_profile(".pdf"), chunk=make_chunk_profile())
            await session.execute(text("UPDATE knowledge_documents SET parsing_profile=CAST(:profile AS jsonb) WHERE id=:id"), {"id": scope[2], "profile": profile.model_dump_json()})
            # A legacy character segment remains legal, but has no image binding.
            await segment(session, scope, None, index_text="", token_count=0)
            for table, column, invalid in [
                ("knowledge_documents", "parsing_profile", "'[]'::jsonb"),
                ("knowledge_documents", "parse_warnings", "'{}'::jsonb"),
                ("knowledge_documents", "source_sha256", "'invalid'"),
                ("knowledge_documents", "upload_state", "'unknown'"),
                ("knowledge_documents", "quota_state", "'released'"),
                ("knowledge_segments", "source_spans", "'{}'::jsonb"),
                ("knowledge_segments", "token_count", "-1"),
            ]:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(text(f"UPDATE {table} SET {column}={invalid}"))
            await session.execute(text("INSERT INTO knowledge_system_settings (id) VALUES (1)"))
            settings = (await session.execute(text("SELECT etl_type,extraction_cache_enabled FROM knowledge_system_settings"))).one()
            assert settings == ("dify", True)
            await session.execute(text("UPDATE knowledge_system_settings SET etl_type='unstructured_local',extraction_cache_enabled=false"))
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text("UPDATE knowledge_system_settings SET etl_type='remote'"))


@pytest.mark.asyncio
async def test_extraction_schema_catalog_signature(postgres_database_url):

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        async with sessions() as session:
            signature = await read_schema_v1_catalog_signature(await session.connection())
    actual = {key: asdict(value) for key, value in signature.items()}
    assert signature == FINAL_SCHEMA_V1_CATALOG_SIGNATURE, f"Schema V1 actual catalog: {json.dumps(actual, sort_keys=True)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("different_base", [False, True])
async def test_same_project_different_document_binding_is_rejected(postgres_database_url, different_base):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            other_base = uuid.uuid4() if different_base else scope[1]
            if different_base:
                await session.execute(
                    text("""INSERT INTO knowledge_bases (id,project_id,name,embedding_model_id)
                    SELECT :id, project_id, 'Sibling Base', embedding_model_id FROM knowledge_bases WHERE id=:base_id"""),
                    {"id": other_base, "base_id": scope[1]},
                )
            other_document = uuid.uuid4()
            await insert_row(
                session,
                "knowledge_documents",
                dict(
                    id=other_document,
                    project_id=scope[0],
                    knowledge_base_id=other_base,
                    name="sibling.pdf",
                    original_name="sibling.pdf",
                    storage_key=f"sources/{other_document}",
                    size_bytes=64,
                    upload_state="stored",
                    quota_state="committed",
                ),
            )
            other_scope = scope[0], other_base, other_document
            eid = await extraction(session, scope)
            other_eid = await extraction(session, other_scope)
            aid = await attachment(session, scope, eid)
            other_aid = await attachment(session, other_scope, other_eid)
            sid = await segment(session, scope, eid)
            await publish(session, scope[2], eid)
            await publish(session, other_document, other_eid)
            for generation, image in [(eid, other_aid), (other_eid, other_aid)]:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await occurrence(session, scope, generation, sid, image)
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await attachment(session, other_scope, eid)
            await occurrence(session, scope, eid, sid, aid)
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await publish(session, other_document, eid)


@pytest.mark.asyncio
async def test_legacy_segment_child_defaults_have_no_implicit_index_backfill(postgres_database_url):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        async with sessions() as session, session.begin():
            sid = uuid.uuid4()
            await insert_row(session, "knowledge_segments", dict(id=sid, **scope_values(scope), document_version=1, position=1, content="legacy body"))
            child_id = uuid.uuid4()
            await insert_row(session, "knowledge_segment_children", dict(id=child_id, **scope_values(scope), knowledge_segment_id=sid, document_version=1, position=1, content="legacy child", embedding="[0.1,0.2]"))
            for table in ["knowledge_segments", "knowledge_segment_children"]:
                assert (await session.execute(text(f"SELECT index_text,token_count,source_spans FROM {table}"))).one() == ("", 0, [])
                for column, invalid in [("token_count", "-1"), ("source_spans", "'{}'::jsonb")]:
                    with pytest.raises(IntegrityError):
                        async with session.begin_nested():
                            await session.execute(text(f"UPDATE {table} SET {column}={invalid}"))


@pytest.mark.asyncio
async def test_extraction_orm_types_and_fk_actions_match_installed_schema(postgres_database_url):
    import sqlalchemy as sa
    from actweave_knowledge.persistence.models import KnowledgeOrmBase

    from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow

    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        async with sessions() as session:
            connection = await session.connection()
            tables = [
                KnowledgeOrmBase.metadata.tables[name]
                for name in ("knowledge_documents", "knowledge_extractions", "knowledge_attachments", "knowledge_segments", "knowledge_segment_children", "knowledge_segment_attachments", "knowledge_tasks")
            ]
            tables.append(KnowledgeSystemSettingsRow.__table__)
            for table in tables:
                reflected = await connection.run_sync(lambda sync: sa.inspect(sync).get_columns(table.name))
                columns = {column["name"]: column for column in reflected}
                for column in table.columns:
                    assert str(column.type.compile(dialect=connection.dialect)) == str(columns[column.name]["type"].compile(dialect=connection.dialect)), (table.name, column.name)
                fks = await connection.run_sync(lambda sync: sa.inspect(sync).get_foreign_keys(table.name))
                reflected_fk = {fk["name"]: fk for fk in fks}
                for fk in table.foreign_key_constraints:
                    actual = reflected_fk[fk.name]
                    assert actual["constrained_columns"] == list(fk.column_keys)
                    assert actual["referred_columns"] == [element.column.name for element in fk.elements]
                    assert actual["options"].get("deferrable", False) == bool(fk.deferrable)
                    assert actual["options"].get("initially") == fk.initially
                    assert actual["options"].get("ondelete") == fk.ondelete


@pytest.mark.asyncio
async def test_creation_claim_has_single_generation(postgres_database_url):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        task_id, token = uuid.uuid4(), uuid.uuid4()
        async with sessions() as session, session.begin():
            await extraction(session, scope, created_task_id=task_id, created_attempt=1, created_claim_token=token)
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await extraction(session, scope, created_task_id=task_id, created_attempt=1, created_claim_token=token)


@pytest.mark.asyncio
async def test_same_attempt_new_claim_preserves_both_creation_generations(postgres_database_url):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        scope = await seed_scope(sessions)
        task_id = uuid.uuid4()
        async with sessions() as session, session.begin():
            first = await extraction(session, scope, created_task_id=task_id, created_attempt=3, created_claim_token=uuid.uuid4())
            second = await extraction(session, scope, created_task_id=task_id, created_attempt=3, created_claim_token=uuid.uuid4())
        assert first != second
