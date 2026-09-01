"""Complete manifest/cache invariants with real claims and storage quota."""

import asyncio
import hashlib
from datetime import timedelta

import pytest
from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError
from actweave_knowledge.extraction import ExtractionLimits, HeaderRule
from extraction_test_helpers import extraction_harness, make_extraction_result, write_test_asset
from parsing_test_helpers import make_chunk_profile, make_parse_profile
from sqlalchemy import select

from deerflow.persistence.quotas.model import ProjectUsageCounterRow, ProjectUsageLedgerRow


async def prepared(h, *, assets=(), work_dir=None):
    source = (await h.read_rows())["documents"][0].source_sha256
    profile = make_parse_profile(".pdf")
    reservation = await h.store.begin(h.claim, source_sha256=source, profile=profile)
    for asset in assets:
        await h.store.persist_attachment(reservation, asset, work_dir=work_dir)
    result = make_extraction_result(profile, source_sha256=source, attachments=assets)
    return reservation, result, profile


async def find(h, result, profile):
    return await h.store.find_ready(h.claim, source_sha256=result.source_sha256, profile=profile, limits=ExtractionLimits())


async def quota_state(h):
    async with h.session_factory() as session:
        counter = await session.scalar(select(ProjectUsageCounterRow).where(ProjectUsageCounterRow.project_id == h.project_id, ProjectUsageCounterRow.dimension == "storage_bytes"))
        ledger = list((await session.scalars(select(ProjectUsageLedgerRow.id).where(ProjectUsageLedgerRow.project_id == h.project_id))).all())
        return counter.used, counter.reserved, set(ledger)


@pytest.mark.asyncio
@pytest.mark.parametrize("images", [False, True])
async def test_cache_preserves_pages_warnings_occurrences_without_second_put(postgres_database_url, tmp_path, images):
    async with extraction_harness(postgres_database_url) as h:
        assets = (write_test_asset(tmp_path),) if images else ()
        reservation, result, profile = await prepared(h, assets=assets, work_dir=tmp_path)
        first = await h.store.complete(reservation, result)
        before = await quota_state(h)
        puts = [c for c in h.object_store.calls if c[0] == "put"]
        cached = await find(h, result, profile)
        assert cached == first and cached.result == result
        assert len(cached.result.documents) == 2 and cached.result.documents[1].warnings
        assert cached.result.documents[1].source_spans[0].location == {"page": 2}
        if images:
            assert cached.result.documents[0].attachments[0].source.location["page"] == 1
            assert cached.result.documents[1].attachments[0].source.location["page"] == 2
        assert [c for c in h.object_store.calls if c[0] == "put"] == puts
        assert await quota_state(h) == before
        rows = await h.read_rows()
        assert rows["documents"][0].published_extraction_id is None
        assert rows["tasks"][0].status == "running" and rows["tasks"][0].extraction_id == first.extraction_id
        assert rows["extractions"][0].unpublished_expires_at - rows["extractions"][0].completed_at == timedelta(hours=24)


@pytest.mark.asyncio
@pytest.mark.parametrize("axis", ["source", "extractor_version", "normalization_version", "image_policy_version", "header_rules", "disabled", "chunk"])
async def test_cache_identity_axes(postgres_database_url, axis):
    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h)
        first = await h.store.complete(reservation, result)
        if axis == "source":
            result = result.model_copy(update={"source_sha256": "a" * 64})
        elif axis == "disabled":
            h.store._cache_enabled = False
        elif axis == "chunk":
            assert make_chunk_profile(size=1000, overlap=100) != make_chunk_profile(size=500, overlap=20)
        else:
            profile = profile.model_copy(update={axis: (HeaderRule(mode="none"),) if axis == "header_rules" else "changed-version"})
        assert (await find(h, result, profile)) == (first if axis == "chunk" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["source", "fingerprint", "inventory", "metadata"])
async def test_complete_rejects_identity_or_inventory_before_manifest_put(postgres_database_url, tmp_path, mismatch):
    async with extraction_harness(postgres_database_url) as h:
        asset = write_test_asset(tmp_path)
        reservation, result, _ = await prepared(h, assets=(asset,), work_dir=tmp_path)
        if mismatch == "source":
            result = result.model_copy(update={"source_sha256": "f" * 64})
        elif mismatch == "fingerprint":
            result = result.model_copy(update={"parse_fingerprint": "f" * 64})
        elif mismatch == "inventory":
            result = make_extraction_result(make_parse_profile(".pdf"), source_sha256=result.source_sha256)
        else:
            result = result.model_copy(update={"attachments": (asset.attachment.model_copy(update={"width": 2}),)})
        before = list(h.object_store.calls)
        with pytest.raises(KnowledgeError):
            await h.store.complete(reservation, result)
        assert h.object_store.calls == before
        assert (await h.read_rows())["extractions"][0].state == "staging"


@pytest.mark.asyncio
async def test_manifest_registration_precedes_put_and_quota_is_real(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        reservation, result, _ = await prepared(h)
        gate = h.object_store.pause("put")
        pending = asyncio.create_task(h.store.complete(reservation, result))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            try:
                row = (await h.read_rows())["extractions"][0]
                assert row.manifest_quota_state == "reserved" and row.manifest_upload_state == "pending"
                assert (await quota_state(h))[:2] == (8, row.manifest_size_bytes)
            finally:
                gate.released.set()
            await pending
        row = (await h.read_rows())["extractions"][0]
        assert row.state == "ready" and row.manifest_quota_state == "committed"
        assert (await quota_state(h))[:2] == (8 + row.manifest_size_bytes, 0)
        assert hashlib.sha256(h.object_store.objects[row.manifest_storage_key]).hexdigest() == row.manifest_sha256


@pytest.mark.asyncio
async def test_manifest_quota_failure_never_puts(postgres_database_url):
    async with extraction_harness(postgres_database_url, quota_bytes=8) as h:
        reservation, result, _ = await prepared(h)
        with pytest.raises(KnowledgeError) as error:
            await h.store.complete(reservation, result)
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert h.object_store.calls == []


async def next_claim(h):
    import uuid

    from actweave_knowledge.persistence.models import KnowledgeTaskRow
    from actweave_knowledge.persistence.tasks import claim_next_task, settle_task_success
    from actweave_knowledge.tasks.worker import KnowledgeTaskClaim

    async with h.session_factory() as session, session.begin():
        await settle_task_success(session, h.claim.id, h.claim.claim_token)
        session.add(KnowledgeTaskRow(id=uuid.uuid4(), project_id=h.project_id, resource_id=h.document_id, kind="ingest_document", target_version=1))
        await session.flush()
        task = await claim_next_task(session, lease_seconds=600)
        h.claim = KnowledgeTaskClaim(
            id=task.id, project_id=task.project_id, resource_id=task.resource_id, kind=task.kind, target_version=task.target_version, claim_token=task.claim_token, attempt_count=task.attempt_count, max_attempts=task.max_attempts
        )


async def publish_fixture(h, extraction_id):
    from sqlalchemy import text

    async with h.session_factory() as session, session.begin():
        await session.execute(text("UPDATE knowledge_documents SET published_extraction_id=:e, published_version=version, status='ready' WHERE id=:d"), {"e": extraction_id, "d": h.document_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("fault", ["manifest_missing", "manifest_corrupt", "asset_missing", "asset_corrupt", "inventory"])
async def test_verified_corruption_is_miss_preserves_publication_and_allows_new_result(postgres_database_url, tmp_path, published, fault):
    from sqlalchemy import text

    async with extraction_harness(postgres_database_url) as h:
        asset = write_test_asset(tmp_path)
        reservation, result, profile = await prepared(h, assets=(asset,), work_dir=tmp_path)
        original = await h.store.complete(reservation, result)
        if published:
            await publish_fixture(h, original.extraction_id)
        await next_claim(h)
        rows = await h.read_rows()
        key = rows["extractions"][0].manifest_storage_key if fault.startswith("manifest") else rows["attachments"][0].storage_key
        if fault.endswith("missing"):
            del h.object_store.objects[key]
        elif fault.endswith("corrupt"):
            data = h.object_store.objects[key]
            h.object_store.objects[key] = bytes([data[0] ^ 1]) + data[1:]
        else:
            async with h.session_factory() as session, session.begin():
                await session.execute(text("UPDATE knowledge_attachments SET width=2 WHERE extraction_id=:e"), {"e": original.extraction_id})
        assert await find(h, result, profile) is None
        rows = await h.read_rows()
        assert rows["documents"][0].published_extraction_id == (original.extraction_id if published else None)
        assert next(t for t in rows["tasks"] if t.id == h.claim.id).extraction_id is None
        assert rows["extractions"][0].state == ("ready" if published else "deleting")
        assert len([t for t in rows["tasks"] if t.kind == "delete_extraction"]) == (0 if published else 1)
        replacement_reservation, replacement_result, _ = await prepared(h)
        replacement = await h.store.complete(replacement_reservation, replacement_result)
        await publish_fixture(h, replacement.extraction_id)
        assert (await h.read_rows())["documents"][0].published_extraction_id == replacement.extraction_id


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["transport", "budget", "expired_lease", "inactive_project"])
async def test_non_corruption_errors_never_fallback_or_clear_pin(postgres_database_url, fault):
    from sqlalchemy import text

    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h)
        original = await h.store.complete(reservation, result)
        await next_claim(h)
        gate = h.object_store.pause("get")
        if fault == "transport":
            h.object_store.fail_next("get")
        if fault == "budget":
            key = (await h.read_rows())["extractions"][0].manifest_storage_key
            h.object_store.objects[key] = b"x" * (ExtractionLimits().max_manifest_bytes + 1)
        pending = asyncio.create_task(find(h, result, profile))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            try:
                rows = await h.read_rows()
                assert next(t for t in rows["tasks"] if t.id == h.claim.id).extraction_id == original.extraction_id
                if fault in ("expired_lease", "inactive_project"):
                    async with h.session_factory() as session, session.begin():
                        sql = "UPDATE knowledge_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id" if fault == "expired_lease" else "UPDATE projects SET status='pending_deletion' WHERE id=:id"
                        await session.execute(text(sql), {"id": h.claim.id if fault == "expired_lease" else h.project_id})
            finally:
                gate.released.set()
            with pytest.raises(KnowledgeError):
                await pending
        rows = await h.read_rows()
        assert rows["extractions"][0].state == "ready"
        assert next(t for t in rows["tasks"] if t.id == h.claim.id).extraction_id == original.extraction_id
        assert not any(t.kind == "delete_extraction" for t in rows["tasks"])


@pytest.mark.asyncio
async def test_download_pin_blocks_cleanup_until_task_settles(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h)
        original = await h.store.complete(reservation, result)
        await next_claim(h)
        gate = h.object_store.pause("get")
        pending = asyncio.create_task(find(h, result, profile))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            try:
                with pytest.raises(KnowledgeError):
                    await h.store.enqueue_cleanup(original.extraction_id, project_id=h.project_id)
            finally:
                gate.released.set()
            assert await pending == original


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_cache_expiration_uses_database_time_but_published_never_expires(postgres_database_url, published):
    from sqlalchemy import text

    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h)
        original = await h.store.complete(reservation, result)
        if published:
            await publish_fixture(h, original.extraction_id)
        await next_claim(h)
        async with h.session_factory() as session, session.begin():
            await session.execute(text("UPDATE knowledge_extractions SET unpublished_expires_at=clock_timestamp()-interval '1 second' WHERE id=:e"), {"e": original.extraction_id})
        assert await find(h, result, profile) == (original if published else None)


@pytest.mark.asyncio
async def test_complete_queues_old_unpublished_result_and_keeps_current_publication(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        old = await h.published_result()
        await next_claim(h)
        reservation, result, _ = await prepared(h)
        middle = await h.store.complete(reservation, result)
        await next_claim(h)
        reservation, result, _ = await prepared(h)
        latest = await h.store.complete(reservation, result)
        rows = await h.read_rows()
        states = {r.id: r.state for r in rows["extractions"]}
        assert states == {old.extraction_id: "ready", middle.extraction_id: "deleting", latest.extraction_id: "ready"}
        assert rows["documents"][0].published_extraction_id == old.extraction_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["put", "get", "cancel", "lease"])
async def test_failed_complete_keeps_physical_quota_facts_and_old_publication(postgres_database_url, failure):
    from sqlalchemy import text

    async with extraction_harness(postgres_database_url) as h:
        old = await h.published_result()
        await next_claim(h)
        reservation, result, _ = await prepared(h)
        gate = h.object_store.pause("put")
        if failure in ("put", "get"):
            h.object_store.fail_next(failure)
        pending = asyncio.create_task(h.store.complete(reservation, result))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            if failure == "cancel":
                pending.cancel()
                await asyncio.get_running_loop().run_in_executor(None, lambda: None)
                assert not pending.done()
            elif failure == "lease":
                async with h.session_factory() as session, session.begin():
                    await session.execute(text("UPDATE knowledge_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:t"), {"t": h.claim.id})
            gate.released.set()
            with pytest.raises((KnowledgeError, asyncio.CancelledError)):
                await pending
        rows = await h.read_rows()
        failed = next(r for r in rows["extractions"] if r.id == reservation.extraction_id)
        assert failed.state == "deleting"
        assert failed.manifest_quota_state == ("reserved" if failure == "put" else "committed")
        assert rows["documents"][0].published_extraction_id == old.extraction_id


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, "", "not-a-sha256"])
async def test_untrusted_asset_hash_metadata_requires_actual_bounded_get(postgres_database_url, tmp_path, monkeypatch, metadata):
    from actweave_knowledge.storage.minio_store import StoredObjectInfo

    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h, assets=(write_test_asset(tmp_path),), work_dir=tmp_path)
        original = await h.store.complete(reservation, result)
        await next_claim(h)
        real_stat = h.object_store.stat_object

        async def without_hash(key):
            info = await real_stat(key)
            return StoredObjectInfo(info.size_bytes, metadata)

        monkeypatch.setattr(h.object_store, "stat_object", without_hash)
        before = len(h.object_store.calls)
        assert await find(h, result, profile) == original
        assert len([c for c in h.object_store.calls[before:] if c[0] == "get"]) == 3
        key = (await h.read_rows())["attachments"][0].storage_key
        h.object_store.objects[key] = b"x" * len(h.object_store.objects[key])
        assert await find(h, result, profile) is None


@pytest.mark.asyncio
async def test_caller_limits_cannot_raise_global_cache_budget(postgres_database_url):
    import json

    from sqlalchemy import text

    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h)
        await h.store.complete(reservation, result)
        row = (await h.read_rows())["extractions"][0]
        envelope = json.loads(h.object_store.objects[row.manifest_storage_key])
        envelope["result"]["documents"][0]["page_content"] = "x" * (ExtractionLimits().max_text_chars + 1)
        payload = json.dumps(envelope, ensure_ascii=False).encode()
        h.object_store.objects[row.manifest_storage_key] = payload
        async with h.session_factory() as session, session.begin():
            await session.execute(text("UPDATE knowledge_extractions SET manifest_size_bytes=:size, manifest_sha256=:sha WHERE id=:id"), {"size": len(payload), "sha": hashlib.sha256(payload).hexdigest(), "id": row.id})
        with pytest.raises(KnowledgeError) as error:
            await h.store.find_ready(h.claim, source_sha256=result.source_sha256, profile=profile, limits=ExtractionLimits(max_text_chars=6_000_000))
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert (await h.read_rows())["extractions"][0].state == "ready"


@pytest.mark.asyncio
async def test_published_cache_fixture_is_settled_and_path_free(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        stored = await h.published_result()
        rows = await h.read_rows()
        assert rows["documents"][0].published_extraction_id == stored.extraction_id
        assert rows["tasks"][0].status == "succeeded" and rows["tasks"][0].extraction_id is None
        assert not any(hasattr(stored, name) for name in ("work_dir", "manifest_path", "storage_key"))


@pytest.mark.asyncio
async def test_complete_does_not_collect_older_cache_with_pending_task_pin(postgres_database_url):
    import uuid

    from actweave_knowledge.persistence.models import KnowledgeTaskRow

    async with extraction_harness(postgres_database_url) as h:
        reservation, result, _ = await prepared(h)
        old = await h.store.complete(reservation, result)
        await next_claim(h)
        async with h.session_factory() as session, session.begin():
            session.add(KnowledgeTaskRow(id=uuid.uuid4(), project_id=h.project_id, resource_id=h.document_id, kind="summarize_document", target_version=2, extraction_id=old.extraction_id, status="queued"))
        reservation, result, _ = await prepared(h)
        latest = await h.store.complete(reservation, result)
        rows = await h.read_rows()
        assert {r.id: r.state for r in rows["extractions"]} == {old.extraction_id: "ready", latest.extraction_id: "ready"}
        assert not any(t.kind == "delete_extraction" for t in rows["tasks"])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["miss_cleanup", "final_inventory"])
async def test_cache_claim_expired_during_final_row_lock_wait_never_returns_or_clears_pin(postgres_database_url, tmp_path, phase):
    from sqlalchemy import text

    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h, assets=(write_test_asset(tmp_path),), work_dir=tmp_path)
        original = await h.store.complete(reservation, result)
        await next_claim(h)
        if phase == "miss_cleanup":
            key = (await h.read_rows())["extractions"][0].manifest_storage_key
            del h.object_store.objects[key]
        gate = h.object_store.pause("get")
        pending = asyncio.create_task(find(h, result, profile))
        blocker = h.session_factory()
        try:
            async with asyncio.timeout(10):
                await gate.entered.wait()
                if phase == "final_inventory":
                    asset_gate = h.object_store.pause("get")
                    gate.released.set()
                    gate = asset_gate
                    await gate.entered.wait()
                async with h.session_factory() as session, session.begin():
                    await session.execute(text("UPDATE knowledge_tasks SET lease_until=clock_timestamp()+interval '2 seconds' WHERE id=:id"), {"id": h.claim.id})
                await blocker.begin()
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                table = "knowledge_extractions" if phase == "miss_cleanup" else "knowledge_attachments"
                await blocker.execute(text(f"SELECT id FROM {table} WHERE {'id' if phase == 'miss_cleanup' else 'extraction_id'}=:id FOR UPDATE"), {"id": original.extraction_id})
                gate.released.set()
                while True:
                    async with h.session_factory() as session:
                        waiting = await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid=ANY(pg_blocking_pids(pid)))"), {"pid": blocker_pid})
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                # This is an actual PostgreSQL lease deadline, not an I/O race
                # synthesized by sleep; the blocking wait was observed above.
                await asyncio.sleep(2.1)
                await blocker.rollback()
                with pytest.raises(KnowledgeError):
                    await pending
            rows = await h.read_rows()
            assert next(t for t in rows["tasks"] if t.id == h.claim.id).extraction_id == original.extraction_id
            assert rows["extractions"][0].state == "ready"
        finally:
            gate.released.set()
            await blocker.close()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
