"""Real host quota settlement and mixed Knowledge storage reconciliation."""

import uuid

import pytest
from actweave_knowledge.contracts import KNOWLEDGE_CONFLICT, KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError
from actweave_knowledge.persistence.models import KnowledgeAttachmentRow, KnowledgeDocumentRow, KnowledgeExtractionRow
from extraction_test_helpers import extraction_harness, installed_knowledge_sessions, seed_scope, test_quota_hash
from sqlalchemy import select

from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.quotas.model import ProjectQuotaRow, ProjectUsageCounterRow, ProjectUsageLedgerRow


@pytest.mark.asyncio
async def test_reconcile_keeps_stored_knowledge_original(postgres_database_url):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        project_id, _, _ = await seed_scope(sessions)
        quotas = QuotaService(session_factory=sessions, config=QuotaConfig(), source_ref_hasher=test_quota_hash)
        async with sessions() as session, session.begin():
            await ProjectQuotaEnforcer(quotas).reconcile_project_storage(session, project_id)
            counter = await session.scalar(select(ProjectUsageCounterRow).where(ProjectUsageCounterRow.project_id == project_id, ProjectUsageCounterRow.dimension == "storage_bytes"))
            assert (counter.used, counter.reserved) == (64, 0)


async def counter_and_ledger(h):
    async with h.session_factory() as s:
        c = await s.scalar(select(ProjectUsageCounterRow).where(ProjectUsageCounterRow.project_id == h.project_id, ProjectUsageCounterRow.dimension == "storage_bytes"))
        ledger = list((await s.scalars(select(ProjectUsageLedgerRow).where(ProjectUsageLedgerRow.project_id == h.project_id))).all())
        return (c.used, c.reserved), [(r.source_kind, r.delta, r.idempotency_key) for r in ledger]


async def reserve_attachment(h, size=17):
    object_id = await h.register_test_attachment(size_bytes=size)
    async with h.session_factory() as s, s.begin():
        await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=size)
    return object_id


async def set_upload_state(h, object_id, state):
    async with h.session_factory() as s, s.begin():
        row = await s.get(KnowledgeAttachmentRow, object_id)
        row.upload_state = state


@pytest.mark.asyncio
async def test_commit_moves_axes_and_reconcile_keeps_knowledge(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        object_id = await reserve_attachment(h)
        async with h.session_factory() as s, s.begin():
            await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)
        assert (await counter_and_ledger(h))[0] == (8, 17)
        await set_upload_state(h, object_id, "stored")
        async with h.session_factory() as s, s.begin():
            await h.quota.commit(s, object_id=object_id)
            await h.quota.commit(s, object_id=object_id)
        before = await counter_and_ledger(h)
        assert before[0] == (25, 0)
        assert sum(kind == "storage_commit_debit" for kind, _, _ in before[1]) == 2
        assert sum(delta for _, delta, _ in before[1]) == 25
        assert all(delta != 0 for _, delta, _ in before[1])
        for _ in range(2):
            async with h.session_factory() as s, s.begin():
                await ProjectQuotaEnforcer(h.quota_service).reconcile_project_storage(s, h.project_id)
        assert await counter_and_ledger(h) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["size", "project", "missing", "collision"])
async def test_reserve_requires_one_exact_object_fact(postgres_database_url, mismatch):
    async with extraction_harness(postgres_database_url) as h:
        object_id = await h.register_test_attachment(size_bytes=17)
        if mismatch == "collision":
            async with h.session_factory() as s, s.begin():
                s.add(KnowledgeDocumentRow(id=object_id, project_id=h.project_id, knowledge_base_id=h.base_id, name="collision", original_name="collision.txt", storage_key=f"collision/{object_id}", size_bytes=17))
        before = await counter_and_ledger(h)
        with pytest.raises(KnowledgeError) as caught:
            async with h.session_factory() as s, s.begin():
                await h.quota.reserve(s, project_id=uuid.uuid4() if mismatch == "project" else h.project_id, object_id=uuid.uuid4() if mismatch == "missing" else object_id, size_bytes=18 if mismatch == "size" else 17)
        assert caught.value.code == KNOWLEDGE_CONFLICT
        assert str(object_id) not in str(caught.value)
        assert await counter_and_ledger(h) == before


@pytest.mark.asyncio
async def test_insufficient_quota_rejects_before_object_io(postgres_database_url):
    async with extraction_harness(postgres_database_url, quota_bytes=10) as h:
        object_id = await h.register_test_attachment(size_bytes=17)
        with pytest.raises(KnowledgeError) as caught:
            async with h.session_factory() as s, s.begin():
                await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)
        assert caught.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert h.object_store.calls == []
        assert (await h.read_rows())["attachments"][0].quota_state == "unreserved"
        assert (await counter_and_ledger(h))[0] == (8, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_release_needs_confirmed_delete_and_reconcile_repairs_crash(postgres_database_url, committed):
    async with extraction_harness(postgres_database_url) as h:
        object_id = await reserve_attachment(h)
        if committed:
            await set_upload_state(h, object_id, "stored")
            async with h.session_factory() as s, s.begin():
                await h.quota.commit(s, object_id=object_id)
        await set_upload_state(h, object_id, "delete_pending")
        before = await counter_and_ledger(h)
        with pytest.raises(KnowledgeError) as caught:
            async with h.session_factory() as s, s.begin():
                await h.quota.release(s, object_id=object_id)
        assert caught.value.code == KNOWLEDGE_CONFLICT
        assert await counter_and_ledger(h) == before
        await set_upload_state(h, object_id, "deleted")
        for _ in range(2):
            async with h.session_factory() as s, s.begin():
                await ProjectQuotaEnforcer(h.quota_service).reconcile_project_storage(s, h.project_id)
        after = await counter_and_ledger(h)
        assert after[0] == (8, 0)
        assert sum(kind == "release" and delta == -17 for kind, delta, _ in after[1]) == 1
        assert (await h.read_rows())["attachments"][0].quota_state == "released"
        async with h.session_factory() as s, s.begin():
            await h.quota.release(s, object_id=object_id)
        with pytest.raises(KnowledgeError):
            async with h.session_factory() as s, s.begin():
                await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)
        assert await counter_and_ledger(h) == after


@pytest.mark.asyncio
async def test_commit_after_limit_tightening_does_not_readmit(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        object_id = await reserve_attachment(h)
        await set_upload_state(h, object_id, "stored")
        async with h.session_factory() as s, s.begin():
            s.add(ProjectQuotaRow(project_id=h.project_id, storage_bytes_limit=0))
        async with h.session_factory() as s, s.begin():
            await h.quota.commit(s, object_id=object_id)
        assert (await counter_and_ledger(h))[0] == (25, 0)


@pytest.mark.asyncio
async def test_zero_byte_object_keeps_facts_without_zero_ledger(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        object_id = uuid.uuid4()
        before = await counter_and_ledger(h)
        async with h.session_factory() as s, s.begin():
            s.add(KnowledgeDocumentRow(id=object_id, project_id=h.project_id, knowledge_base_id=h.base_id, name="empty", original_name="empty.txt", storage_key=f"empty/{object_id}", size_bytes=0))
            await s.flush()
            await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=0)
            row = await s.get(KnowledgeDocumentRow, object_id)
            row.upload_state = "stored"
            await h.quota.commit(s, object_id=object_id)
            row.upload_state = "deleted"
            await h.quota.release(s, object_id=object_id)
            assert row.quota_state == "released"
        assert await counter_and_ledger(h) == before


@pytest.mark.asyncio
async def test_operator_reconciliation_keeps_axes_and_preview_is_readonly(postgres_database_url):
    from app.quotas.models import _issue_quota_reconciliation_authority
    from app.quotas.reconciliation import QuotaReconciler

    async with extraction_harness(postgres_database_url) as h:
        object_id = await reserve_attachment(h)
        authority = _issue_quota_reconciliation_authority(h.project_id, operation="quota_repair")
        operator = QuotaReconciler(h.session_factory, h.quota_service)
        before = await counter_and_ledger(h)
        report = await operator.preview(authority)
        assert all(d.dimension != "storage_bytes" for d in report.differences)
        assert await counter_and_ledger(h) == before
        await set_upload_state(h, object_id, "deleted")
        report = await operator.preview(authority)
        assert [(d.current, d.expected) for d in report.differences if d.dimension == "storage_bytes"] == [(25, 8)]
        assert await counter_and_ledger(h) == before
        assert (await h.read_rows())["attachments"][0].quota_state == "reserved"
        await operator.execute(authority)
        assert (await counter_and_ledger(h))[0] == (8, 0)
        assert (await h.read_rows())["attachments"][0].quota_state == "released"
        after = await counter_and_ledger(h)
        await operator.execute(authority)
        assert await counter_and_ledger(h) == after


@pytest.mark.asyncio
async def test_mixed_knowledge_manifest_and_delete_pending_keep_exact_axes(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        pending = await reserve_attachment(h, 11)
        stored = await reserve_attachment(h, 17)
        deleting = await reserve_attachment(h, 23)
        for object_id in (stored, deleting):
            await set_upload_state(h, object_id, "stored")
            async with h.session_factory() as s, s.begin():
                await h.quota.commit(s, object_id=object_id)
        await set_upload_state(h, deleting, "delete_pending")
        async with h.session_factory() as s, s.begin():
            attachment = await s.get(KnowledgeAttachmentRow, pending)
            manifest = await s.get(KnowledgeExtractionRow, attachment.extraction_id)
            manifest.manifest_storage_key = f"manifests/{manifest.id}"
            manifest.manifest_sha256 = "c" * 64
            manifest.manifest_size_bytes = 29
            await h.quota.reserve(s, project_id=h.project_id, object_id=manifest.id, size_bytes=29)
            manifest.manifest_upload_state = "stored"
            await h.quota.commit(s, object_id=manifest.id)
            assert manifest.state == "staging"
        before = await counter_and_ledger(h)
        assert before[0] == (77, 11)
        for _ in range(2):
            async with h.session_factory() as s, s.begin():
                await ProjectQuotaEnforcer(h.quota_service).reconcile_project_storage(s, h.project_id)
        assert await counter_and_ledger(h) == before


@pytest.mark.asyncio
async def test_commit_refuses_pending_and_missing_exact_reservation(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        object_id = await reserve_attachment(h)
        before = await counter_and_ledger(h)
        with pytest.raises(KnowledgeError):
            async with h.session_factory() as s, s.begin():
                await h.quota.commit(s, object_id=object_id)
        assert await counter_and_ledger(h) == before
        unreserved = await h.register_test_attachment(size_bytes=17, upload_state="stored")
        async with h.session_factory() as s, s.begin():
            row = await s.get(KnowledgeAttachmentRow, unreserved)
            row.quota_state = "reserved"
        with pytest.raises(KnowledgeError):
            async with h.session_factory() as s, s.begin():
                await h.quota.commit(s, object_id=unreserved)
        assert await counter_and_ledger(h) == before


@pytest.mark.asyncio
async def test_storage_commit_requires_atomic_exact_ledger_pair(postgres_database_url):
    from app.quotas.models import QuotaConflict, _issue_project_storage_quota_authority
    from deerflow.persistence.quotas.sql import QuotaRepository

    async with extraction_harness(postgres_database_url) as h:
        object_id = await reserve_attachment(h)
        key = f"knowledge-object:{object_id}"
        authority = _issue_project_storage_quota_authority(h.project_id, operation="commit")
        before = await counter_and_ledger(h)
        with pytest.raises(QuotaConflict):
            async with h.session_factory() as s, s.begin():
                await h.quota_service.commit_project_storage(s, authority, 18, key)
        assert await counter_and_ledger(h) == before
        async with h.session_factory() as s, s.begin():
            # An incomplete historical pair must fail closed, never manufacture
            # its other half or debit the aggregate a second time.
            ref = h.quota_service._source_ref(project_id=h.project_id, owner_user_id="trusted:project_storage", dimension="storage_bytes", bucket="lifetime", operation="storage_commit_debit", key=key)
            await QuotaRepository(s).append_ledger(
                project_id=h.project_id,
                dimension="storage_bytes",
                delta=-17,
                bucket="lifetime",
                source_kind="storage_commit_debit",
                source_ref_key_id=ref.key_id,
                source_ref_hmac=ref.hmac_hex,
                idempotency_key=h.quota_service._idempotency_digest(source_ref=ref),
                request_id=None,
                occurred_at=h.quota_service._now(None),
            )
        before = await counter_and_ledger(h)
        with pytest.raises(QuotaConflict):
            async with h.session_factory() as s, s.begin():
                await h.quota_service.commit_project_storage(s, authority, 17, key)
        assert await counter_and_ledger(h) == before


@pytest.mark.asyncio
async def test_concurrent_reservation_of_one_object_is_once(postgres_database_url):
    import asyncio

    async with extraction_harness(postgres_database_url) as h:
        object_id = await h.register_test_attachment(size_bytes=17)
        ready = asyncio.Event()

        async def reserve():
            await ready.wait()
            async with h.session_factory() as s, s.begin():
                await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)

        tasks = [asyncio.create_task(reserve()) for _ in range(2)]
        ready.set()
        await asyncio.gather(*tasks)
        counters, ledger = await counter_and_ledger(h)
        assert counters == (8, 17)
        assert sum(kind == "reserve" and delta == 17 for kind, delta, _ in ledger) == 1


@pytest.mark.asyncio
async def test_storage_reconciliation_int_compatibility_and_nonzero_axis_repairs(postgres_database_url):
    from app.quotas.models import StorageUsageTotals, _issue_quota_reconciliation_authority

    async with extraction_harness(postgres_database_url) as h:
        authority = _issue_quota_reconciliation_authority(h.project_id, operation="quota_repair")

        async def legacy():
            return 8

        async def knowledge():
            return StorageUsageTotals(used=8, reserved=0)

        async with h.session_factory() as s, s.begin():
            await h.quota_service.reconcile_project_storage(s, authority, expected_loader=legacy)
        assert (await counter_and_ledger(h))[0] == (0, 8)
        async with h.session_factory() as s, s.begin():
            await h.quota_service.reconcile_project_storage(s, authority, expected_loader=knowledge)
        counters, ledger = await counter_and_ledger(h)
        assert counters == (8, 0)
        assert all(delta for _, delta, _ in ledger)
        assert sum(delta for _, delta, _ in ledger) == 8


@pytest.mark.asyncio
async def test_quota_policy_unavailable_maps_safe_error(postgres_database_url):
    from actweave_knowledge.contracts import KNOWLEDGE_STORAGE_UNAVAILABLE

    from app.knowledge.quota_port import HostKnowledgeStorageQuotaPort
    from app.quotas.models import QuotaUnavailable

    class UnavailablePolicy:
        async def read_current_quotas(self, session):
            raise QuotaUnavailable()

    async with extraction_harness(postgres_database_url) as h:
        unavailable = QuotaService(h.session_factory, QuotaConfig(), source_ref_hasher=test_quota_hash, current_policy_reader=UnavailablePolicy())
        port = HostKnowledgeStorageQuotaPort(unavailable)
        object_id = await h.register_test_attachment(size_bytes=17)
        with pytest.raises(KnowledgeError) as caught:
            async with h.session_factory() as s, s.begin():
                await port.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)
        assert caught.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        assert (await counter_and_ledger(h))[0] == (8, 0)


@pytest.mark.asyncio
async def test_parallel_uploads_keep_callers_project_shared_lock(postgres_database_url):
    import asyncio

    from deerflow.persistence.projects.model import ProjectRow

    async with extraction_harness(postgres_database_url) as h:
        ids = [await h.register_test_attachment(size_bytes=17) for _ in range(2)]
        both_locked = asyncio.Event()
        locked = 0

        async def reserve(object_id):
            nonlocal locked
            async with h.session_factory() as s, s.begin():
                # Business admission already holds the host's Project SHARE
                # fence. Two admitted uploads must not upgrade it to UPDATE.
                await s.scalar(select(ProjectRow).where(ProjectRow.id == h.project_id).with_for_update(read=True))
                locked += 1
                if locked == 2:
                    both_locked.set()
                await both_locked.wait()
                await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)

        results = await asyncio.gather(*(reserve(object_id) for object_id in ids), return_exceptions=True)
        assert not any(isinstance(result, Exception) for result in results)
        assert (await counter_and_ledger(h))[0] == (8, 34)


@pytest.mark.parametrize(
    "arguments",
    [
        ("-c", "import app.knowledge.gateway"),
        ("-m", "pytest", "tests/knowledge/test_storage.py", "--collect-only", "-q", "--tb=short"),
    ],
    ids=["gateway-import", "storage-collection"],
)
def test_knowledge_gateway_loads_in_fresh_process_without_quota_import_cycle(arguments):
    import os
    import subprocess
    import sys
    from pathlib import Path

    environment = dict(os.environ)
    environment.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
