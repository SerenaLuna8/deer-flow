"""M3 gates: MinIO object store behavior and the blocking-I/O static gate.

Integration tests run against the local development MinIO and skip when the
``ACT_WEAVE_KNOWLEDGE_MINIO_*`` environment values are absent; the storage-key
and event-loop gates always run.
"""

from __future__ import annotations

import ast
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from actweave_knowledge import KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from actweave_knowledge.contracts import KnowledgeMinioSettings
from actweave_knowledge.storage import MinioObjectStore, document_storage_key
from actweave_knowledge.storage import minio_store as minio_store_module

import app.knowledge.gateway as knowledge_gateway_module

_ENDPOINT = os.environ.get("ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT", "")
_ACCESS_KEY = os.environ.get("ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY", "")
_SECRET_KEY = os.environ.get("ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY", "")

requires_minio = pytest.mark.skipif(
    not (_ENDPOINT and _ACCESS_KEY and _SECRET_KEY),
    reason="ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY are required for MinIO integration tests",
)


def _settings(bucket: str, **overrides: object) -> KnowledgeMinioSettings:
    values: dict[str, object] = {
        "endpoint": _ENDPOINT,
        "bucket": bucket,
        "access_key": _ACCESS_KEY,
        "secret_key": _SECRET_KEY,
        "secure": False,
    }
    values.update(overrides)
    return KnowledgeMinioSettings.model_validate(values)


@pytest.fixture()
def minio_bucket() -> Iterator[str]:
    """A throwaway bucket in the development MinIO, removed after the test."""

    from minio import Minio

    admin = Minio(_ENDPOINT, access_key=_ACCESS_KEY, secret_key=_SECRET_KEY, secure=False)
    bucket = f"knowledge-test-{uuid.uuid4().hex[:12]}"
    admin.make_bucket(bucket)
    try:
        yield bucket
    finally:
        for entry in admin.list_objects(bucket, recursive=True):
            admin.remove_object(bucket, entry.object_name)
        admin.remove_bucket(bucket)


# ---------------------------------------------------------------------------
# Storage key
# ---------------------------------------------------------------------------


def test_document_storage_key_uses_ids_and_lowercased_extension() -> None:
    project_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    base_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    document_id = uuid.UUID("33333333-3333-4333-8333-333333333333")

    key = document_storage_key(project_id, base_id, document_id, "季度报告.PDF")

    assert key == f"projects/{project_id}/knowledge/{base_id}/{document_id}.pdf"
    # The user-controlled filename must never appear in the key.
    assert "季度报告" not in key


# ---------------------------------------------------------------------------
# MinIO integration (env-gated)
# ---------------------------------------------------------------------------


@requires_minio
@pytest.mark.asyncio
async def test_round_trip_between_two_stores_preserves_bytes(minio_bucket: str, tmp_path: Path) -> None:
    """The Gateway store writes; a separate Worker store reads the same key."""

    payload = b"knowledge round trip \xe6\xb5\x8b\xe8\xaf\x95" * 1024
    source = tmp_path / "source.pdf"
    source.write_bytes(payload)
    target = tmp_path / "downloaded.pdf"

    gateway_store = MinioObjectStore(_settings(minio_bucket))
    worker_store = MinioObjectStore(_settings(minio_bucket))
    key = document_storage_key(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "source.pdf")

    await gateway_store.upload_from(key, source, media_type="application/pdf")
    await worker_store.download_to(key, target)

    assert target.read_bytes() == payload


@requires_minio
@pytest.mark.asyncio
async def test_delete_removes_object_and_is_idempotent(minio_bucket: str, tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"delete me")
    store = MinioObjectStore(_settings(minio_bucket))
    key = document_storage_key(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "source.txt")
    await store.upload_from(key, source)

    await store.delete(key)
    # Deleting an absent object still succeeds (S3 semantics).
    await store.delete(key)

    with pytest.raises(KnowledgeError) as error:
        await store.download_to(key, tmp_path / "missing.txt")
    assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE


@requires_minio
@pytest.mark.asyncio
async def test_download_of_missing_object_is_storage_unavailable(minio_bucket: str, tmp_path: Path) -> None:
    store = MinioObjectStore(_settings(minio_bucket))

    with pytest.raises(KnowledgeError) as error:
        await store.download_to("projects/none/knowledge/none/none.txt", tmp_path / "missing.txt")

    assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE


@requires_minio
@pytest.mark.asyncio
async def test_check_bucket_requires_existing_bucket_and_valid_credentials(minio_bucket: str) -> None:
    assert await MinioObjectStore(_settings(minio_bucket)).check_bucket() is True
    assert await MinioObjectStore(_settings(f"missing-{uuid.uuid4().hex[:12]}")).check_bucket() is False
    wrong_secret = _settings(minio_bucket, secret_key="wrong-secret-key")
    assert await MinioObjectStore(wrong_secret).check_bucket() is False


# ---------------------------------------------------------------------------
# Blocking-I/O static gate
# ---------------------------------------------------------------------------

# Calls that would block the event loop when made directly inside an async
# function. Wrapping them with asyncio.to_thread passes the callable as an
# argument (not a Call node), and nested sync helpers run inside the thread,
# so neither triggers a violation.
_MINIO_BLOCKING_CALLS = frozenset({"fput_object", "fget_object", "remove_object", "bucket_exists", "list_buckets"})
_FILE_BLOCKING_CALLS = frozenset({"open", "write", "unlink", "close", "mkstemp", "mkdtemp", "NamedTemporaryFile", "read_bytes", "write_bytes"})


def _direct_calls_in_async_functions(source_path: Path, blocked: frozenset[str]) -> list[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        stack: list[ast.AST] = list(node.body)
        while stack:
            current = stack.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # nested defs are separate execution contexts
            if isinstance(current, ast.Call):
                target = current.func
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name in blocked:
                    violations.append(f"{source_path.name}:{current.lineno} {node.name} calls {name}() directly")
            stack.extend(ast.iter_child_nodes(current))
    return violations


def test_minio_store_never_calls_the_sync_client_on_the_event_loop() -> None:
    module_path = Path(minio_store_module.__file__)
    assert _direct_calls_in_async_functions(module_path, _MINIO_BLOCKING_CALLS | _FILE_BLOCKING_CALLS) == []


def test_knowledge_gateway_never_does_sync_file_io_on_the_event_loop() -> None:
    module_path = Path(knowledge_gateway_module.__file__)
    assert _direct_calls_in_async_functions(module_path, _MINIO_BLOCKING_CALLS | _FILE_BLOCKING_CALLS) == []
