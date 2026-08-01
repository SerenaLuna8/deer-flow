from __future__ import annotations

import asyncio
import errno
import hashlib
import threading
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.errors import PrivateWorkInvalid, PrivateWorkTooLarge, PrivateWorkUnavailable
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef

MIB = 1024 * 1024


def test_finalization_file_limit_is_independent_from_upload_batch_limit() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizationLimits
    from app.private_work.file_service import PrivateFileLimits
    from deerflow.workspace_changes.types import WorkspaceChangeLimits

    assert PrivateFileLimits().max_files == 10
    assert PrivateFileFinalizationLimits().max_scanned_files == 2000
    assert PrivateFileFinalizationLimits().max_scanned_files == WorkspaceChangeLimits().max_scanned_files
    assert PrivateFileFinalizationLimits().max_scanned_files != WorkspaceChangeLimits().max_files


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), 127_997):
        await asyncio.sleep(0)
        yield payload[offset : offset + 127_997]


class MemorySecureSandbox:
    def __init__(self, files: dict[str, bytes], *, symlinks: tuple[str, ...] = (), directories: tuple[str, ...] = ()) -> None:
        self.files = dict(files)
        self.symlinks = symlinks
        self.directories = directories
        self._reads: dict[str, tuple[bytes, int]] = {}
        self.max_read = 0
        self.closed: list[str] = []
        self.order: list[str] = []

    def list_secure_files(self, root: str, *, max_entries: int):
        from deerflow.sandbox.sandbox import SandboxFileInfo

        self.order.append("scan")
        entries = [SandboxFileInfo(path=path, size=len(payload), file_type="regular") for path, payload in self.files.items() if path.startswith(root.rstrip("/") + "/")]
        entries.extend(SandboxFileInfo(path=path, size=0, file_type="symlink") for path in self.symlinks if path.startswith(root.rstrip("/") + "/"))
        entries.extend(SandboxFileInfo(path=path, size=0, file_type="directory") for path in self.directories if path.startswith(root.rstrip("/") + "/"))
        if len(entries) > max_entries:
            raise OSError(errno.EFBIG, "secure scan limit exceeded")
        return tuple(sorted(entries, key=lambda item: item.path))

    def open_regular_file(self, path: str) -> str:
        if path in self.symlinks:
            raise OSError("symlink")
        handle = uuid.uuid4().hex
        self._reads[handle] = (self.files[path], 0)
        return handle

    def read_regular_file(self, handle: str, max_bytes: int) -> bytes:
        assert max_bytes == MIB
        payload, offset = self._reads[handle]
        result = payload[offset : offset + max_bytes]
        self._reads[handle] = (payload, offset + len(result))
        self.max_read = max(self.max_read, len(result))
        return result

    def close_regular_file(self, handle: str) -> None:
        self._reads.pop(handle, None)
        self.closed.append(handle)


def test_finalizer_default_scan_accepts_more_files_than_one_upload_batch() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer

    files = {f"/mnt/user-data/workspace/file-{index}.txt": b"x" for index in range(11)}
    finalizer = PrivateFileFinalizer(SimpleNamespace())
    run_scope = SimpleNamespace(context=SimpleNamespace(request_id="finalization-limit-test"))

    scanned = finalizer._scan(run_scope, MemorySecureSandbox(files))

    assert len(scanned) == 11


def test_finalizer_default_scan_accepts_exact_scan_limit() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizationLimits, PrivateFileFinalizer

    limit = PrivateFileFinalizationLimits().max_scanned_files
    files = {f"/mnt/user-data/workspace/file-{index:04d}.txt": b"x" for index in range(limit)}
    finalizer = PrivateFileFinalizer(SimpleNamespace())
    run_scope = SimpleNamespace(context=SimpleNamespace(request_id="finalization-exact-scan-limit-test"))

    scanned = finalizer._scan(run_scope, MemorySecureSandbox(files))

    assert len(scanned) == limit


def test_finalizer_file_limit_does_not_count_directory_entries() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizationLimits, PrivateFileFinalizer

    limit = PrivateFileFinalizationLimits().max_scanned_files
    files = {f"/mnt/user-data/workspace/dir-{index % 5}/file-{index:04d}.txt": b"x" for index in range(limit)}
    directories = tuple(f"/mnt/user-data/workspace/dir-{index}" for index in range(5))
    finalizer = PrivateFileFinalizer(SimpleNamespace())
    run_scope = SimpleNamespace(context=SimpleNamespace(request_id="finalization-directory-entry-test"))

    scanned = finalizer._scan(run_scope, MemorySecureSandbox(files, directories=directories))

    assert len(scanned) == limit


def test_finalizer_scan_entry_limit_is_global_across_roots() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizationLimits, PrivateFileFinalizer

    finalizer = PrivateFileFinalizer(
        SimpleNamespace(),
        limits=PrivateFileFinalizationLimits(
            max_scanned_files=1,
            max_scan_entries=2,
        ),
    )
    run_scope = SimpleNamespace(context=SimpleNamespace(request_id="finalization-global-entry-limit-test"))
    sandbox = MemorySecureSandbox(
        {"/mnt/user-data/workspace/file.txt": b"x"},
        directories=(
            "/mnt/user-data/workspace/dir",
            "/mnt/user-data/outputs/dir",
        ),
    )

    with pytest.raises(PrivateWorkTooLarge):
        finalizer._scan(run_scope, sandbox)


def test_finalizer_default_scan_rejects_one_file_beyond_scan_limit() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizationLimits, PrivateFileFinalizer

    limit = PrivateFileFinalizationLimits().max_scanned_files
    files = {f"/mnt/user-data/workspace/file-{index:04d}.txt": b"x" for index in range(limit + 1)}
    finalizer = PrivateFileFinalizer(SimpleNamespace())
    run_scope = SimpleNamespace(context=SimpleNamespace(request_id="finalization-over-scan-limit-test"))

    with pytest.raises(PrivateWorkTooLarge):
        finalizer._scan(run_scope, MemorySecureSandbox(files))


def test_finalizer_scan_does_not_apply_changed_byte_budget_to_restored_tree() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizationLimits, PrivateFileFinalizer

    finalizer = PrivateFileFinalizer(
        SimpleNamespace(),
        limits=PrivateFileFinalizationLimits(
            max_scanned_files=2,
            max_scan_entries=2,
            max_changed_file_size=1,
            max_changed_total_size=1,
        ),
    )
    run_scope = SimpleNamespace(context=SimpleNamespace(request_id="finalization-restored-tree-budget-test"))

    scanned = finalizer._scan(
        run_scope,
        MemorySecureSandbox(
            {
                "/mnt/user-data/workspace/existing-a.txt": b"x",
                "/mnt/user-data/workspace/existing-b.txt": b"y",
            }
        ),
    )

    assert len(scanned) == 2


def test_finalizer_changed_byte_budget_accepts_exact_limit_and_rejects_over_limit() -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizationLimits, PrivateFileFinalizer, _AfterFile

    finalizer = PrivateFileFinalizer(
        SimpleNamespace(),
        limits=PrivateFileFinalizationLimits(
            max_scanned_files=2,
            max_scan_entries=2,
            max_changed_file_size=2,
            max_changed_total_size=2,
        ),
    )
    run_scope = SimpleNamespace(context=SimpleNamespace(request_id="finalization-changed-budget-test"))
    first = _AfterFile("workspace/a.txt", "/mnt/user-data/workspace/a.txt", "workspace", 1, "text/plain")
    second = _AfterFile("workspace/b.txt", "/mnt/user-data/workspace/b.txt", "workspace", 1, "text/plain")

    finalizer._validate_changed_files(run_scope, (first, second))

    over_total = _AfterFile("workspace/c.txt", "/mnt/user-data/workspace/c.txt", "workspace", 1, "text/plain")
    with pytest.raises(PrivateWorkTooLarge):
        finalizer._validate_changed_files(run_scope, (first, second, over_total))

    over_single_file = _AfterFile("workspace/large.txt", "/mnt/user-data/workspace/large.txt", "workspace", 3, "text/plain")
    with pytest.raises(PrivateWorkTooLarge):
        finalizer._validate_changed_files(run_scope, (over_single_file,))


class FailingReadSandbox(MemorySecureSandbox):
    def read_regular_file(self, handle: str, max_bytes: int) -> bytes:
        raise OSError("sandbox read failed")


class BlockingReadSandbox(MemorySecureSandbox):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(files)
        self.read_started = threading.Event()
        self.allow_read = threading.Event()

    def read_regular_file(self, handle: str, max_bytes: int) -> bytes:
        self.read_started.set()
        if not self.allow_read.wait(2):
            raise TimeoutError("test did not release sandbox read")
        return super().read_regular_file(handle, max_bytes)


class BlockingOpenSandbox(MemorySecureSandbox):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(files)
        self.open_started = threading.Event()
        self.allow_open = threading.Event()

    def open_regular_file(self, path: str) -> str:
        handle = super().open_regular_file(path)
        self.open_started.set()
        if not self.allow_open.wait(2):
            raise TimeoutError("test did not release sandbox open")
        return handle


class MutatingSecondScanSandbox(MemorySecureSandbox):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(files)
        self._root_scans = 0

    def list_secure_files(self, root: str, *, max_entries: int):
        self._root_scans += 1
        if self._root_scans == 3:
            self.files["/mnt/user-data/outputs/report.txt"] = b"changed"
        return super().list_secure_files(root, max_entries=max_entries)


async def _seed_staging_sentinel(seed, thread_id: str, run_id: str) -> uuid.UUID:
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    sentinel_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        await PrivateFileRepository(session).stage(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            kind="output",
            logical_path=f"outputs/.sentinel-{sentinel_id.hex}",
            media_type="application/octet-stream",
            created_by_run_id=run_id,
            file_id=sentinel_id,
        )
    return sentinel_id


async def _staging_ids(seed, run_id: str) -> tuple[uuid.UUID, ...]:
    async with seed.engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT id FROM files WHERE created_by_run_id=:run_id AND status='staging' ORDER BY id"),
                {"run_id": run_id},
            )
        ).scalars()
        return tuple(rows)


def _manifest_entry(file):
    from app.private_work.sandbox_files import AuthorityManifestEntry

    return AuthorityManifestEntry(
        file_id=file.id,
        logical_path=file.logical_path,
        kind=file.kind,
        media_type=file.media_type,
        size=file.size,
        sha256=file.sha256,
        version=file.version,
    )


@pytest_asyncio.fixture()
async def finalizer_seed(migrated_postgres_database_url: str):
    from app.private_work.file_service import PrivateFileService

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"finalizer-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    origin_trace_id = "test-private-file-finalizer-trace"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    old = await PrivateFileService(seed.factory).upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="workspace/draft.txt",
        media_type="text/plain",
        chunks=_chunks(b"old"),
    )
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO runs
                (run_id,thread_id,project_id,owner_user_id,origin_trace_id,status,
                 multitask_strategy,
                 metadata_json,kwargs_json,finalization_status,
                 message_count,total_input_tokens,total_output_tokens,total_tokens,
                 llm_call_count,lead_agent_tokens,subagent_tokens,middleware_tokens,
                 token_usage_by_model,created_at,updated_at)
                VALUES (:run_id,:thread_id,:project_id,:owner,
                        :origin_trace_id,'running','reject',
                        '{}'::json,'{}'::json,'pending',0,0,0,0,0,0,0,0,
                        '{}'::json,now(),now())"""
            ),
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "project_id": seed.owner_a.project_id,
                "owner": seed.owner_a_scope.owner_user_id,
                "origin_trace_id": origin_trace_id,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO jobs
                (id,job_type,project_id,owner_user_id,run_id,origin_trace_id,
                 idempotency_key,status,max_attempts)
                VALUES (:job_id,'private_run',:project_id,:owner,:run_id,
                        :origin_trace_id,:idempotency_key,'queued',3)"""
            ),
            {
                "job_id": job_id,
                "project_id": seed.owner_a.project_id,
                "owner": seed.owner_a_scope.owner_user_id,
                "run_id": run_id,
                "origin_trace_id": origin_trace_id,
                "idempotency_key": hashlib.sha256(f"finalizer:{run_id}".encode()).hexdigest(),
            },
        )
        await connection.execute(
            text(
                """UPDATE runs SET job_id=:job_id
                WHERE project_id=:project_id AND owner_user_id=:owner
                AND run_id=:run_id"""
            ),
            {
                "job_id": job_id,
                "project_id": seed.owner_a.project_id,
                "owner": seed.owner_a_scope.owner_user_id,
                "run_id": run_id,
            },
        )
    try:
        yield seed, thread_id, run_id, old
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_authorizes_then_atomically_supersedes_and_creates_artifact(finalizer_seed) -> None:
    from app.audit.service import AuditService, _bind_worker_audit_process
    from app.audit.sinks import OperationalAuditSink
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import (
        AuthorityManifest,
        AuthorityManifestEntry,
        PrivateFileRunScope,
    )
    from app.reliability.owner_refs import AuditHmacKeyring

    seed, thread_id, run_id, old = finalizer_seed
    boundary = SimpleNamespace(
        before_file_finalization=AsyncMock(),
        before_file_finalization_in_session=AsyncMock(),
    )
    sandbox = MemorySecureSandbox(
        {
            "/mnt/user-data/workspace/draft.txt": b"new" * (MIB // 3 + 5),
            "/mnt/user-data/outputs/report.txt": b"presented output",
        }
    )
    manifest = AuthorityManifest(
        entries=(
            AuthorityManifestEntry(
                file_id=old.id,
                logical_path=old.logical_path,
                kind=old.kind,
                media_type=old.media_type,
                size=old.size,
                sha256=old.sha256,
                version=old.version,
            ),
        )
    )

    audit_service = AuditService(
        seed.factory,
        AuditHmacKeyring.from_environment(),
    )
    audit = OperationalAuditSink(
        audit_service,
        process_context=_bind_worker_audit_process(audit_service),
    )
    result = await PrivateFileFinalizer(seed.factory, audit=audit).finalize(
        PrivateFileRunScope(
            seed.owner_a,
            thread_id=thread_id,
            run_id=run_id,
            authorization_boundary=boundary,
        ),
        manifest,
        sandbox,
        presented_paths=("/mnt/user-data/outputs/report.txt",),
    )

    boundary.before_file_finalization.assert_awaited_once()
    assert boundary.before_file_finalization_in_session.await_count >= 4
    assert sandbox.order == ["scan", "scan", "scan", "scan"]
    assert sandbox.max_read == MIB
    assert len(result.files) == 2
    assert len(result.artifacts) == 1
    assert result.workspace_changes == {
        "created": ["outputs/report.txt"],
        "modified": ["workspace/draft.txt"],
        "deleted": [],
    }
    async with seed.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """SELECT id,status,version,source_file_id,sha256
                    FROM files WHERE project_id=:project_id AND owner_user_id=:owner
                    AND thread_id=:thread_id AND logical_path='workspace/draft.txt'
                    ORDER BY version"""
                ),
                {
                    "project_id": seed.owner_a.project_id,
                    "owner": seed.owner_a_scope.owner_user_id,
                    "thread_id": thread_id,
                },
            )
        ).all()
        finalization_status = await connection.scalar(
            text("SELECT finalization_status FROM runs WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
        artifact = (
            await connection.execute(
                text("SELECT deleted_at,file_id FROM artifacts WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
        ).one()
        audit_row = (
            await connection.execute(
                text(
                    """SELECT actor_process,action,target_kind,request_id,job_id,
                    metadata_json,target_ref_hmac
                    FROM audit_logs
                    WHERE project_id=:project_id
                    AND action='run.files_finalized'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        ).one()
    assert rows[0][1] == "deleted"
    assert rows[1][1:4] == ("ready", 2, old.id)
    assert rows[1][4] == hashlib.sha256(sandbox.files["/mnt/user-data/workspace/draft.txt"]).hexdigest()
    assert finalization_status == "complete"
    assert artifact.deleted_at is None
    assert audit_row.actor_process == "worker"
    assert audit_row.action == "run.files_finalized"
    assert audit_row.target_kind == "run"
    assert audit_row.request_id != "test-private-file-finalizer-trace"
    assert len(audit_row.request_id) == 64
    assert audit_row.job_id is not None
    assert audit_row.metadata_json == {
        "created_count": 1,
        "modified_count": 1,
        "deleted_count": 0,
        "artifact_count": 1,
        "committed_bytes": len(sandbox.files["/mnt/user-data/workspace/draft.txt"]) + len(sandbox.files["/mnt/user-data/outputs/report.txt"]),
    }
    assert run_id not in audit_row.target_ref_hmac
    assert not {
        "path",
        "name",
        "file_id",
        "body",
        "locator",
        "sha256",
    }.intersection(audit_row.metadata_json)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_rolls_back_files_artifact_and_audit_when_audit_append_fails(
    finalizer_seed,
) -> None:
    from app.audit.service import AuditService, _bind_worker_audit_process
    from app.audit.sinks import OperationalAuditSink
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import (
        AuthorityManifest,
        PrivateFileRunScope,
    )
    from app.reliability.owner_refs import AuditHmacKeyring

    seed, thread_id, run_id, old = finalizer_seed
    sandbox = MemorySecureSandbox(
        {
            "/mnt/user-data/workspace/draft.txt": b"changed",
            "/mnt/user-data/outputs/report.txt": b"presented",
        }
    )
    manifest = AuthorityManifest(entries=(_manifest_entry(old),))
    boundary = SimpleNamespace(
        before_file_finalization=AsyncMock(),
        before_file_finalization_in_session=AsyncMock(),
    )
    audit_service = AuditService(
        seed.factory,
        AuditHmacKeyring.from_environment(),
    )
    audit = OperationalAuditSink(
        audit_service,
        process_context=_bind_worker_audit_process(audit_service),
    )

    class FailAfterAuditAppend:
        async def run_files_finalized(self, *args, **kwargs) -> None:
            await audit.run_files_finalized(*args, **kwargs)
            raise RuntimeError("injected failure after audit append")

    scope = PrivateFileRunScope(
        seed.owner_a,
        thread_id=thread_id,
        run_id=run_id,
        authorization_boundary=boundary,
    )
    with pytest.raises(PrivateWorkUnavailable):
        await PrivateFileFinalizer(
            seed.factory,
            audit=FailAfterAuditAppend(),
        ).finalize(
            scope,
            manifest,
            sandbox,
            presented_paths=("/mnt/user-data/outputs/report.txt",),
        )

    async with seed.engine.connect() as connection:
        failed_snapshot = (
            await connection.execute(
                text(
                    """SELECT
                    (SELECT status FROM files WHERE id=:old_id) AS old_status,
                    (SELECT count(*) FROM files
                     WHERE created_by_run_id=:run_id) AS run_file_count,
                    (SELECT count(*) FROM artifacts
                     WHERE run_id=:run_id) AS artifact_count,
                    (SELECT count(*) FROM audit_logs
                     WHERE action='run.files_finalized'
                     AND project_id=:project_id) AS audit_count,
                    (SELECT finalization_status FROM runs
                     WHERE run_id=:run_id) AS finalization_status"""
                ),
                {
                    "old_id": old.id,
                    "run_id": run_id,
                    "project_id": seed.owner_a.project_id,
                },
            )
        ).one()
    assert failed_snapshot == ("ready", 0, 0, 0, "failed")

    await PrivateFileFinalizer(seed.factory, audit=audit).finalize(
        scope,
        manifest,
        sandbox,
        presented_paths=("/mnt/user-data/outputs/report.txt",),
    )
    async with seed.engine.connect() as connection:
        committed_snapshot = (
            await connection.execute(
                text(
                    """SELECT
                    (SELECT count(*) FROM audit_logs
                     WHERE action='run.files_finalized'
                     AND project_id=:project_id) AS audit_count,
                    (SELECT count(*) FROM artifacts
                     WHERE run_id=:run_id) AS artifact_count,
                    (SELECT finalization_status FROM runs
                     WHERE run_id=:run_id) AS finalization_status"""
                ),
                {
                    "run_id": run_id,
                    "project_id": seed.owner_a.project_id,
                },
            )
        ).one()
    assert committed_snapshot == (1, 1, "complete")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_releases_superseded_file_and_reserves_promoted_file_in_one_transaction(
    finalizer_seed,
) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope
    from app.quotas.integration import ProjectQuotaEnforcer
    from app.quotas.service import QuotaService
    from app.reliability.owner_refs import AuditHmacKeyring
    from deerflow.config.quota_config import QuotaConfig

    seed, thread_id, run_id, old = finalizer_seed
    keyring = AuditHmacKeyring.from_environment()
    enforcer = ProjectQuotaEnforcer(
        QuotaService(
            seed.factory,
            QuotaConfig(default_storage_bytes_limit=10),
            source_ref_hasher=keyring.quota_source_ref,
        )
    )
    async with seed.factory() as session, session.begin():
        await enforcer.reserve_file(
            session,
            seed.owner_a,
            file_id=old.id,
            size=old.size,
        )

    result = await PrivateFileFinalizer(
        seed.factory,
        quota=enforcer,
    ).finalize(
        PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
        AuthorityManifest(entries=(_manifest_entry(old),)),
        MemorySecureSandbox({"/mnt/user-data/workspace/draft.txt": b"new"}),
    )

    assert len(result.files) == 1
    async with seed.factory() as session:
        state = (
            await session.execute(
                text(
                    """SELECT reserved,
                              (SELECT count(*) FROM project_usage_ledger
                               WHERE project_id=:project_id
                                 AND dimension='storage_bytes'
                                 AND source_kind='release') AS releases
                       FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'
                         AND bucket='lifetime'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        ).one()
    assert tuple(state) == (3, 1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_persists_unpresented_output_without_creating_artifact(finalizer_seed) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, old = finalizer_seed
    sandbox = MemorySecureSandbox(
        {
            "/mnt/user-data/outputs/presented.txt": b"presented",
            "/mnt/user-data/outputs/internal.txt": b"internal",
        }
    )

    result = await PrivateFileFinalizer(seed.factory).finalize(
        PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
        AuthorityManifest(entries=(_manifest_entry(old),)),
        sandbox,
        presented_paths=("/mnt/user-data/outputs/presented.txt",),
    )

    assert {row.logical_path for row in result.files} == {
        "outputs/internal.txt",
        "outputs/presented.txt",
    }
    assert [row.metadata["logical_path"] for row in result.artifacts] == ["outputs/presented.txt"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_creates_current_run_artifact_for_unchanged_presented_output(
    finalizer_seed,
) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.file_service import PrivateFileService
    from app.private_work.sandbox_files import (
        AuthorityManifest,
        PrivateFileRunScope,
    )

    seed, thread_id, run_id, old = finalizer_seed
    ready = await PrivateFileService(seed.factory).upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="outputs/unchanged.txt",
        media_type="text/plain",
        chunks=_chunks(b"unchanged"),
    )
    manifest = AuthorityManifest(entries=(_manifest_entry(old), _manifest_entry(ready)))

    result = await PrivateFileFinalizer(seed.factory).finalize(
        PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
        manifest,
        MemorySecureSandbox({"/mnt/user-data/outputs/unchanged.txt": b"unchanged"}),
        presented_paths=("/mnt/user-data/outputs/unchanged.txt",),
    )

    assert result.files == ()
    assert len(result.artifacts) == 1
    assert result.artifacts[0].file_id == ready.id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_rejects_presented_path_missing_from_verified_after_manifest(
    finalizer_seed,
) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, _old = finalizer_seed

    with pytest.raises(PrivateWorkInvalid):
        await PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            AuthorityManifest(entries=()),
            MemorySecureSandbox({"/mnt/user-data/outputs/actual.txt": b"actual"}),
            presented_paths=("/mnt/user-data/outputs/missing.txt",),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_second_secure_scan_rejects_exact_manifest_drift(finalizer_seed) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, _old = finalizer_seed
    sandbox = MutatingSecondScanSandbox({"/mnt/user-data/outputs/report.txt": b"initial"})

    with pytest.raises(PrivateWorkInvalid):
        await PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            AuthorityManifest(entries=()),
            sandbox,
        )

    assert sandbox.order == ["scan", "scan", "scan", "scan"]
    assert await _staging_ids(seed, run_id) == ()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_rejects_nfc_collision_before_any_staging(finalizer_seed) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, _old = finalizer_seed
    sandbox = MemorySecureSandbox(
        {
            "/mnt/user-data/outputs/caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt": b"nfc",
            "/mnt/user-data/outputs/cafe\N{COMBINING ACUTE ACCENT}.txt": b"nfd",
        }
    )

    finalizer = PrivateFileFinalizer(seed.factory)
    finalizer._stage_file = AsyncMock(side_effect=AssertionError("NFC collision reached staging"))

    with pytest.raises(PrivateWorkInvalid):
        await finalizer.finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            AuthorityManifest(entries=()),
            sandbox,
        )

    finalizer._stage_file.assert_not_awaited()
    assert sandbox._reads == {}
    assert await _staging_ids(seed, run_id) == ()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["upload", "delete"])
async def test_finalizer_optimistic_authority_compare_rejects_concurrent_change(
    finalizer_seed,
    concurrent_change: str,
) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.file_service import PrivateFileService
    from app.private_work.sandbox_files import (
        AuthorityManifest,
        AuthorityManifestEntry,
        PrivateFileRunScope,
    )

    seed, thread_id, run_id, old = finalizer_seed
    manifest = AuthorityManifest(
        entries=(
            AuthorityManifestEntry(
                file_id=old.id,
                logical_path=old.logical_path,
                kind=old.kind,
                media_type=old.media_type,
                size=old.size,
                sha256=old.sha256,
                version=old.version,
            ),
        )
    )
    service = PrivateFileService(seed.factory)
    if concurrent_change == "upload":
        concurrent = await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="workspace/concurrent.txt",
            media_type="text/plain",
            chunks=_chunks(b"concurrent"),
        )
    else:
        concurrent = await service.delete_ready(
            seed.owner_a,
            thread_id=thread_id,
            file_id=old.id,
        )

    with pytest.raises(PrivateWorkUnavailable):
        await PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            manifest,
            MemorySecureSandbox({"/mnt/user-data/workspace/draft.txt": b"old"}),
        )

    assert await _staging_ids(seed, run_id) == ()
    async with seed.engine.connect() as connection:
        current = (
            await connection.execute(
                text("SELECT id,status FROM files WHERE id=:file_id"),
                {"file_id": concurrent.id},
            )
        ).one_or_none()
    if concurrent_change == "upload":
        assert current == (concurrent.id, "ready")
    else:
        assert current is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_user_delete_physically_removes_artifact_metadata_and_file_bytes(
    finalizer_seed,
) -> None:
    from app.private_work.file_service import PrivateFileService
    from deerflow.persistence.private_work.model import PrivateArtifactRow

    seed, thread_id, run_id, ready = finalizer_seed
    artifact_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            PrivateArtifactRow(
                id=artifact_id,
                project_id=ready.project_id,
                owner_user_id=ready.owner_user_id,
                thread_id=thread_id,
                run_id=run_id,
                file_id=ready.id,
                display_name="private-report.txt",
                media_type="text/plain",
                artifact_metadata={"private": "metadata"},
            )
        )

    deleted = await PrivateFileService(seed.factory).delete_ready(
        seed.owner_a,
        thread_id=thread_id,
        file_id=ready.id,
    )
    assert deleted.status == "deleted"
    async with seed.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM artifacts WHERE id=:artifact_id"),
                {"artifact_id": artifact_id},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM files WHERE id=:file_id"),
                {"file_id": ready.id},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM file_chunks WHERE file_id=:file_id"),
                {"file_id": ready.id},
            )
            == 0
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_rejects_symlink_and_persists_allowed_failed_status(finalizer_seed) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, _old = finalizer_seed
    boundary = SimpleNamespace(before_file_finalization=AsyncMock())
    sandbox = MemorySecureSandbox(
        {},
        symlinks=("/mnt/user-data/outputs/leak.txt",),
    )

    with pytest.raises(PrivateWorkInvalid):
        await PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(
                seed.owner_a,
                thread_id=thread_id,
                run_id=run_id,
                authorization_boundary=boundary,
            ),
            AuthorityManifest(entries=()),
            sandbox,
        )

    boundary.before_file_finalization.assert_awaited_once()
    async with seed.engine.connect() as connection:
        status = await connection.scalar(
            text("SELECT finalization_status FROM runs WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
        staging = await connection.scalar(
            text("SELECT count(*) FROM files WHERE created_by_run_id=:run_id AND status='staging'"),
            {"run_id": run_id},
        )
    assert status == "failed"
    assert staging == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_read_failure_cleans_only_its_exact_staging_id(finalizer_seed) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, _old = finalizer_seed
    sentinel_id = await _seed_staging_sentinel(seed, thread_id, run_id)
    sandbox = FailingReadSandbox({"/mnt/user-data/outputs/new.txt": b"new"})

    with pytest.raises(PrivateWorkUnavailable):
        await PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            AuthorityManifest(entries=()),
            sandbox,
        )

    assert await _staging_ids(seed, run_id) == (sentinel_id,)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_chunk_failure_cleans_only_its_exact_staging_id(
    finalizer_seed,
    monkeypatch,
) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    seed, thread_id, run_id, _old = finalizer_seed
    sentinel_id = await _seed_staging_sentinel(seed, thread_id, run_id)

    async def fail_append_chunk(*_args, **_kwargs):
        raise RuntimeError("chunk insert failed")

    monkeypatch.setattr(PrivateFileRepository, "append_chunk", fail_append_chunk)
    sandbox = MemorySecureSandbox({"/mnt/user-data/outputs/new.txt": b"new"})

    with pytest.raises(PrivateWorkUnavailable):
        await PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            AuthorityManifest(entries=()),
            sandbox,
        )

    assert await _staging_ids(seed, run_id) == (sentinel_id,)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_cancelled_read_joins_and_cleans_only_its_exact_staging_id(
    finalizer_seed,
) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, _old = finalizer_seed
    sentinel_id = await _seed_staging_sentinel(seed, thread_id, run_id)
    sandbox = BlockingReadSandbox({"/mnt/user-data/outputs/new.txt": b"new"})
    task = asyncio.create_task(
        PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            AuthorityManifest(entries=()),
            sandbox,
        )
    )
    assert await asyncio.to_thread(sandbox.read_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    sandbox.allow_read.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _staging_ids(seed, run_id) == (sentinel_id,)
    async with seed.engine.connect() as connection:
        status = await connection.scalar(
            text("SELECT finalization_status FROM runs WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
    assert status == "failed"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalizer_cancelled_during_reader_open_closes_returned_handle(
    finalizer_seed,
) -> None:
    from app.private_work.file_finalizer import PrivateFileFinalizer
    from app.private_work.sandbox_files import AuthorityManifest, PrivateFileRunScope

    seed, thread_id, run_id, _old = finalizer_seed
    sandbox = BlockingOpenSandbox({"/mnt/user-data/outputs/new.txt": b"new"})
    task = asyncio.create_task(
        PrivateFileFinalizer(seed.factory).finalize(
            PrivateFileRunScope(seed.owner_a, thread_id=thread_id, run_id=run_id),
            AuthorityManifest(entries=()),
            sandbox,
        )
    )
    assert await asyncio.to_thread(sandbox.open_started.wait, 2)
    task.cancel()
    sandbox.allow_open.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sandbox._reads == {}
    assert len(sandbox.closed) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_delete_tombstones_files_and_artifacts_in_same_transaction(finalizer_seed) -> None:
    from app.private_work.thread_service import PrivateThreadService
    from deerflow.persistence.private_work.model import PrivateArtifactRow

    seed, thread_id, run_id, old = finalizer_seed
    artifact_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            PrivateArtifactRow(
                id=artifact_id,
                project_id=seed.owner_a.project_id,
                owner_user_id=seed.owner_a_scope.owner_user_id,
                thread_id=thread_id,
                run_id=run_id,
                file_id=old.id,
                display_name="draft.txt",
                media_type="text/plain",
                artifact_metadata={},
            )
        )

    from langgraph.checkpoint.memory import InMemorySaver

    from app.private_work.checkpointer import ProjectScopedCheckpointer

    async with seed.engine.connect() as connection:
        version = await connection.scalar(
            text("SELECT version FROM threads_meta WHERE thread_id=:thread_id"),
            {"thread_id": thread_id},
        )
    await PrivateThreadService(
        seed.factory,
        ProjectScopedCheckpointer(InMemorySaver(), seed.factory),
    ).delete(
        seed.owner_a,
        thread_id,
        expected_version=version,
    )

    async with seed.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """SELECT t.deleted_at AS thread_deleted_at,
                              f.status,
                              f.deleted_at AS file_deleted_at,
                              a.deleted_at AS artifact_deleted_at
                    FROM threads_meta t JOIN files f ON f.thread_id=t.thread_id
                    JOIN artifacts a ON a.file_id=f.id WHERE t.thread_id=:thread_id"""
                ),
                {"thread_id": thread_id},
            )
        ).one()
    assert all(value is not None for value in (row.thread_deleted_at, row.file_deleted_at, row.artifact_deleted_at))
    assert row.status == "deleted"
