from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.worker.memory_extract import MemoryExtractJobHandler
from app.worker.service import JobOutcome, JobSettlement, WorkerService
from deerflow.agents.memory.extractor import (
    ExtractedMemoryCandidate,
    MemoryExtractionInvalid,
    MemoryExtractionResult,
)
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.persistence.private_work.memory_v2_repository import (
    MemoryExtractionModelSnapshot,
    MemoryExtractionSourceItemRecord,
    MemoryExtractionWork,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


class _Authority:
    def __init__(self, *, cancel_requested: bool = False) -> None:
        self.cancel_requested = cancel_requested
        self.heartbeats = 0

    async def heartbeat(self) -> None:
        self.heartbeats += 1


class _Repository:
    def __init__(self, work: MemoryExtractionWork) -> None:
        self.work = work
        self.finalized: list[dict[str, object]] = []

    async def load_extraction_work(self, **_kwargs) -> MemoryExtractionWork:
        return self.work

    async def finalize_extraction(self, **kwargs):
        self.finalized.append(kwargs)
        return SimpleNamespace(status="succeeded")


class _PolicyMaterializer:
    def __init__(self, *, mode: str = "shadow", model_name: str | None = None) -> None:
        self.policy = SimpleNamespace(
            memory=SimpleNamespace(
                enabled=True,
                pipeline_mode=mode,
                model_name=model_name,
            )
        )
        self.calls: list[dict[str, object]] = []

    async def materialize_run_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return self.policy


class _ModelMaterializer:
    def __init__(self) -> None:
        self.model = SimpleNamespace(name="memory-test")
        self.calls: list[dict[str, object]] = []

    async def materialize_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return self.model


class _Extractor:
    def __init__(self, result: MemoryExtractionResult | Exception) -> None:
        self.result = result
        self.calls: list[object] = []

    async def extract(self, sources):
        self.calls.append(sources)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _claim() -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="memory-lease",
        job_type="memory_extract",
        scope=JobScope(uuid.uuid4(), str(uuid.uuid4())),
        run_id=None,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        namespace="default",
    )


def _work(
    claim: JobClaim,
    *,
    suppressed: bool = False,
    cancel_requested: bool = False,
    committed: bool = False,
) -> MemoryExtractionWork:
    model_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    model_checksum = "a" * 64
    return MemoryExtractionWork(
        generation_id=uuid.uuid4(),
        source_batch_id=uuid.uuid4(),
        job_id=claim.job_id,
        project_id=claim.scope.project_id,
        owner_user_id=claim.scope.owner_user_id or "",
        namespace=claim.namespace or "",
        thread_id="thread-1",
        run_id="run-1",
        pipeline_mode="shadow",
        contract_digest="b" * 64,
        policy_revision=1,
        model_config_id=model_id,
        model_config_version_id=model_version_id,
        model_config_checksum=model_checksum,
        prompt_version="memory-extract-prompt-v1",
        extractor_version="memory-extractor-v1",
        output_schema_version="memory-candidate-v1",
        source_items=(
            MemoryExtractionSourceItemRecord(
                id=uuid.uuid4(),
                ordinal=0,
                source_message_id="message-1",
                content="我偏好简洁回答。",
                content_hmac="c" * 64,
            ),
        ),
        model_snapshots=(
            MemoryExtractionModelSnapshot(
                purpose="lead",
                model_config_id=model_id,
                model_config_version_id=model_version_id,
                model_config_checksum=model_checksum,
            ),
        ),
        suppressed=suppressed,
        cancel_requested=cancel_requested,
        candidate_committed=committed,
    )


async def _scope_allowed(_session, _claim, *, lock: bool) -> bool:
    assert isinstance(lock, bool)
    return True


class _ScopeSequence:
    def __init__(self, *results: bool) -> None:
        self._results = list(results)
        self.locks: list[bool] = []

    async def __call__(self, _session, _claim, *, lock: bool) -> bool:
        self.locks.append(lock)
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_memory_extract_handler_calls_model_then_defers_atomic_finalize() -> None:
    claim = _claim()
    work = _work(claim)
    repository = _Repository(work)
    policy = _PolicyMaterializer()
    model = _ModelMaterializer()
    extraction = MemoryExtractionResult(
        candidates=(
            ExtractedMemoryCandidate(
                source_ordinal=0,
                candidate_type="preference",
                content="用户偏好简洁回答。",
                confidence=0.98,
                retention_class="durable",
                sensitivity="normal",
            ),
        ),
    )
    extractor = _Extractor(extraction)
    handler = MemoryExtractJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=model,
        runtime_policy_materializer=policy,
        extractor_factory=lambda _model: extractor,
        repository_builder=lambda _session, **_kwargs: repository,
        scope_validator=_scope_allowed,
    )
    authority = _Authority()

    settlement = await handler(claim, authority)

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome == JobOutcome.succeeded()
    assert authority.heartbeats == 3
    assert model.calls == [
        {
            "project_id": claim.scope.project_id,
            "owner_user_id": claim.scope.owner_user_id,
            "run_id": "run-1",
            "purpose": "lead",
        }
    ]
    assert len(extractor.calls) == 1
    assert repository.finalized == []

    await settlement.commit()

    assert len(repository.finalized) == 1
    finalized = repository.finalized[0]
    assert finalized["job_id"] == claim.job_id
    assert finalized["generation_id"] == work.generation_id
    assert finalized["lease_token"] == "memory-lease"
    assert finalized["pipeline_mode"] == "shadow"
    assert finalized["cancel"] is False
    writes = finalized["candidates"]
    assert len(writes) == 1
    assert writes[0].source_ordinal == 0
    assert writes[0].candidate_type == "preference"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suppressed,cancel_requested,committed",
    [(True, False, False), (False, True, False), (False, False, True)],
)
async def test_memory_extract_handler_skips_model_for_terminal_domain_work(
    suppressed: bool,
    cancel_requested: bool,
    committed: bool,
) -> None:
    claim = _claim()
    work = _work(
        claim,
        suppressed=suppressed,
        cancel_requested=cancel_requested,
        committed=committed,
    )
    repository = _Repository(work)
    model = _ModelMaterializer()
    extractor = _Extractor(MemoryExtractionResult(()))
    handler = MemoryExtractJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=model,
        runtime_policy_materializer=_PolicyMaterializer(),
        extractor_factory=lambda _model: extractor,
        repository_builder=lambda _session, **_kwargs: repository,
        scope_validator=_scope_allowed,
    )

    settlement = await handler(claim, _Authority())
    assert isinstance(settlement, JobSettlement)
    assert model.calls == []
    assert extractor.calls == []

    await settlement.commit()
    assert repository.finalized[0]["candidates"] == ()
    assert repository.finalized[0]["cancel"] is (suppressed or cancel_requested)


@pytest.mark.asyncio
async def test_memory_extract_handler_retries_invalid_model_output() -> None:
    claim = _claim()
    repository = _Repository(_work(claim))
    extractor = _Extractor(
        MemoryExtractionInvalid("MEMORY_EXTRACT_OUTPUT_INVALID"),
    )
    handler = MemoryExtractJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=_PolicyMaterializer(),
        extractor_factory=lambda _model: extractor,
        repository_builder=lambda _session, **_kwargs: repository,
        scope_validator=_scope_allowed,
    )

    result = await handler(claim, _Authority())

    assert result == JobOutcome.failed("MEMORY_EXTRACT_OUTPUT_INVALID")
    assert repository.finalized == []


@pytest.mark.asyncio
async def test_memory_extract_handler_rejects_an_unknown_frozen_contract() -> None:
    claim = _claim()
    repository = _Repository(
        replace(_work(claim), prompt_version="memory-extract-prompt-v0"),
    )
    model = _ModelMaterializer()
    extractor = _Extractor(MemoryExtractionResult(()))
    handler = MemoryExtractJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=model,
        runtime_policy_materializer=_PolicyMaterializer(),
        extractor_factory=lambda _model: extractor,
        repository_builder=lambda _session, **_kwargs: repository,
        scope_validator=_scope_allowed,
    )

    result = await handler(claim, _Authority())

    assert result == JobOutcome.failed("MEMORY_EXTRACT_CONTRACT_UNSUPPORTED")
    assert model.calls == []
    assert extractor.calls == []


@pytest.mark.asyncio
async def test_memory_extract_handler_rechecks_scope_before_commit() -> None:
    claim = _claim()
    repository = _Repository(_work(claim))
    extractor = _Extractor(
        MemoryExtractionResult(
            candidates=(
                ExtractedMemoryCandidate(
                    source_ordinal=0,
                    candidate_type="preference",
                    content="用户偏好简洁回答。",
                    confidence=0.98,
                    retention_class="durable",
                    sensitivity="normal",
                ),
            ),
        )
    )
    scope = _ScopeSequence(True, False)
    handler = MemoryExtractJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=_PolicyMaterializer(),
        extractor_factory=lambda _model: extractor,
        repository_builder=lambda _session, **_kwargs: repository,
        scope_validator=scope,
    )

    settlement = await handler(claim, _Authority())
    await settlement.commit()

    assert scope.locks == [False, True]
    assert repository.finalized[0]["cancel"] is True
    assert repository.finalized[0]["candidates"] == ()


def test_worker_service_accepts_only_the_enabled_memory_handler() -> None:
    async def handler(_claim, _authority):
        return JobOutcome.succeeded()

    WorkerService(
        _SessionFactory(),
        SimpleNamespace(),
        {"memory_extract": handler},
        WorkerConfig(),
    )
    with pytest.raises(ValueError, match="unsupported job type"):
        WorkerService(
            _SessionFactory(),
            SimpleNamespace(),
            {"memory_consolidate": handler},
            WorkerConfig(),
        )
