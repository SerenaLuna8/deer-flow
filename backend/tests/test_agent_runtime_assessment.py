from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import project_assets
from app.private_work.agent_runtime_assessment import (
    AgentRuntimeAssessment,
    AgentRuntimeAssessmentService,
)
from app.private_work.context import require_issued_private_work_context
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedRunAssetClosure,
)
from app.system_settings.repository import SystemModelRepositoryInvariant

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_READY_AGENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_UNAVAILABLE_AGENT_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_DEPENDENCY_AGENT_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_MODEL_AGENT_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
_INACTIVE_MODEL_REF = "88888888-8888-4888-8888-888888888890"
_REQUEST_ID = "agent-runtime-assessment"


def _context() -> ProjectContext:
    role = ProjectRole.ADMIN
    return ProjectContext(
        user_id=_USER_ID,
        project_id=_PROJECT_ID,
        membership_id=_MEMBERSHIP_ID,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=7,
        request_id=_REQUEST_ID,
    )


def _closure(
    agent_id: uuid.UUID,
    *,
    model_ref: str = "default",
) -> ResolvedRunAssetClosure:
    version_id = uuid.uuid5(uuid.NAMESPACE_URL, f"agent-version:{agent_id}")
    lead = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=agent_id,
        version_id=version_id,
        checksum="a" * 64,
        catalog_generation=9,
        dependency_version_ids=(),
        payload=AgentPayload(
            description="Runtime assessment fixture",
            agents_instructions="Run safely.",
            soul="Be precise.",
            identity="Runtime assessor fixture.",
            user_context="Test context.",
            model_ref=model_ref,
            model_settings=AgentModelSettings(),
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )
    return ResolvedRunAssetClosure(
        lead_agent=lead,
        delegated_agents=(),
        skills=(),
        mcps=(),
        main_skill_version_ids=(),
        main_mcp_version_ids=(),
    )


class _TransactionSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return self


class _Resolver:
    async def resolve_run_asset_closure_in_session(
        self,
        _session,
        _context,
        selection,
    ) -> ResolvedRunAssetClosure:
        if selection.asset_id == _UNAVAILABLE_AGENT_ID:
            raise AssetResolutionUnavailable(_REQUEST_ID)
        if selection.asset_id == _MODEL_AGENT_ID:
            return _closure(selection.asset_id, model_ref=_INACTIVE_MODEL_REF)
        return _closure(selection.asset_id)


class _ClosureValidator:
    def __init__(self) -> None:
        self.contexts: list[object] = []

    async def validate_run_asset_closure_in_session(
        self,
        _session,
        context,
        closure: ResolvedRunAssetClosure,
    ) -> tuple[list[object], list[object], dict[object, object], dict[object, object]]:
        require_issued_private_work_context(context)
        self.contexts.append(context)
        if closure.lead_agent.asset_id == _DEPENDENCY_AGENT_ID:
            raise RunSnapshotAssetStale
        return [], [], {}, {}


class _ModelCatalog:
    def __init__(self, *, fail_storage: bool = False) -> None:
        self.fail_storage = fail_storage
        self.calls: list[tuple[str | None, bool]] = []

    async def resolve_active_model(
        self,
        model_ref: str | None,
        *,
        load_envelope: bool,
    ) -> object | None:
        self.calls.append((model_ref, load_envelope))
        if self.fail_storage:
            raise SystemModelRepositoryInvariant
        return object() if model_ref == "default" else None


class _ProjectReadLocker:
    def __init__(self) -> None:
        self.calls: list[tuple[ProjectContext, bool]] = []

    async def lock_project(
        self,
        context: ProjectContext,
        *,
        read: bool = False,
    ) -> None:
        self.calls.append((context, read))


def _service(
    *,
    resolver: object | None = None,
    closure_validator: object | None = None,
    model_catalog: _ModelCatalog | None = None,
) -> tuple[AgentRuntimeAssessmentService, _TransactionSession, _ClosureValidator, _ModelCatalog]:
    session = _TransactionSession()
    validator = closure_validator or _ClosureValidator()
    models = model_catalog or _ModelCatalog()
    project_read_locker = _ProjectReadLocker()
    context_calls: list[tuple[object, ...]] = []

    async def resolve_context(
        received_session,
        user_id,
        project_id,
        request_id,
        *,
        lock: bool,
    ) -> ProjectContext:
        context_calls.append(
            (received_session, user_id, project_id, request_id, lock),
        )
        return _context()

    service = AgentRuntimeAssessmentService(
        lambda: session,  # type: ignore[arg-type]
        resolver=resolver or _Resolver(),  # type: ignore[arg-type]
        closure_validator=validator,  # type: ignore[arg-type]
        model_catalog_factory=lambda received: models if received is session else (_ for _ in ()).throw(AssertionError()),
        project_read_locker_factory=lambda received: project_read_locker if received is session else (_ for _ in ()).throw(AssertionError()),
        context_resolver=resolve_context,
    )
    service.context_calls = context_calls  # type: ignore[attr-defined]
    service.project_read_lock_calls = project_read_locker.calls  # type: ignore[attr-defined]
    return service, session, validator, models  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_batch_assessment_preserves_input_order_and_fails_closed_per_agent() -> None:
    service, session, validator, models = _service()

    result = await service.assess(
        _context(),
        (
            _UNAVAILABLE_AGENT_ID,
            _READY_AGENT_ID,
            _DEPENDENCY_AGENT_ID,
            _MODEL_AGENT_ID,
        ),
    )

    assert result == (
        AgentRuntimeAssessment(
            agent_asset_id=_UNAVAILABLE_AGENT_ID,
            selected_version_id=None,
            status="blocked",
            reason_code="agent_unavailable",
        ),
        AgentRuntimeAssessment(
            agent_asset_id=_READY_AGENT_ID,
            selected_version_id=_closure(_READY_AGENT_ID).lead_agent.version_id,
            status="ready",
            reason_code=None,
        ),
        AgentRuntimeAssessment(
            agent_asset_id=_DEPENDENCY_AGENT_ID,
            selected_version_id=_closure(_DEPENDENCY_AGENT_ID).lead_agent.version_id,
            status="blocked",
            reason_code="runtime_dependency_unavailable",
        ),
        AgentRuntimeAssessment(
            agent_asset_id=_MODEL_AGENT_ID,
            selected_version_id=_closure(_MODEL_AGENT_ID).lead_agent.version_id,
            status="blocked",
            reason_code="model_unavailable",
        ),
    )
    assert service.context_calls == [  # type: ignore[attr-defined]
        (session, _USER_ID, _PROJECT_ID, _REQUEST_ID, False),
    ]
    assert service.project_read_lock_calls == [(_context(), True)]  # type: ignore[attr-defined]
    assert len(validator.contexts) == 3
    assert models.calls == [("default", False), (_INACTIVE_MODEL_REF, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_ids",
    [
        (),
        (_READY_AGENT_ID, _READY_AGENT_ID),
        tuple(uuid.uuid4() for _ in range(101)),
    ],
)
async def test_batch_assessment_rejects_empty_duplicate_or_oversized_input(
    agent_ids: tuple[uuid.UUID, ...],
) -> None:
    service, *_ = _service(
        resolver=SimpleNamespace(
            resolve_run_asset_closure_in_session=lambda *_args: (_ for _ in ()).throw(
                AssertionError("invalid input must not reach the resolver"),
            ),
        ),
    )

    with pytest.raises(AssetValidationFailed):
        await service.assess(_context(), agent_ids)

    assert service.context_calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_batch_assessment_requires_shared_asset_read_before_database_access() -> None:
    service, *_ = _service()

    with pytest.raises(AssetForbidden):
        await service.assess(
            replace(_context(), capabilities=frozenset()),
            (_READY_AGENT_ID,),
        )

    assert service.context_calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_batch_assessment_treats_storage_uncertainty_as_a_whole_request_failure() -> None:
    service, *_ = _service(model_catalog=_ModelCatalog(fail_storage=True))

    with pytest.raises(AssetStorageUnavailable) as caught:
        await service.assess(_context(), (_READY_AGENT_ID,))

    assert caught.value.request_id == _REQUEST_ID


@pytest.mark.asyncio
async def test_batch_assessment_validates_every_delegated_agent_model() -> None:
    lead_closure = _closure(_READY_AGENT_ID)
    delegate = replace(
        lead_closure.lead_agent,
        asset_id=_DEPENDENCY_AGENT_ID,
        version_id=uuid.uuid5(
            uuid.NAMESPACE_URL,
            "delegated-agent-version",
        ),
        payload=replace(
            lead_closure.lead_agent.payload,
            model_ref=_INACTIVE_MODEL_REF,
        ),
    )
    closure = replace(lead_closure, delegated_agents=(delegate,))

    class _DelegatingResolver:
        async def resolve_run_asset_closure_in_session(
            self,
            _session,
            _context,
            _selection,
        ) -> ResolvedRunAssetClosure:
            return closure

    service, *_ = _service(resolver=_DelegatingResolver())

    assert await service.assess(_context(), (_READY_AGENT_ID,)) == (
        AgentRuntimeAssessment(
            agent_asset_id=_READY_AGENT_ID,
            selected_version_id=lead_closure.lead_agent.version_id,
            status="blocked",
            reason_code="model_unavailable",
        ),
    )


class _HttpService:
    async def assess(
        self,
        actor: ProjectContext,
        agent_ids: tuple[uuid.UUID, ...],
    ) -> tuple[AgentRuntimeAssessment, ...]:
        assert actor == _context()
        return tuple(
            AgentRuntimeAssessment(
                agent_asset_id=agent_id,
                selected_version_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"http-assessment:{agent_id}",
                ),
                status="ready",
                reason_code=None,
            )
            for agent_id in agent_ids
        )


@pytest.mark.parametrize(
    ("selected_version_id", "status", "reason_code"),
    [
        (None, "ready", None),
        (_READY_AGENT_ID, "ready", "model_unavailable"),
        (_READY_AGENT_ID, "blocked", "agent_unavailable"),
        (None, "blocked", "runtime_dependency_unavailable"),
        (None, "blocked", None),
    ],
)
def test_runtime_assessment_response_rejects_invalid_state_combinations(
    selected_version_id: uuid.UUID | None,
    status: str,
    reason_code: str | None,
) -> None:
    with pytest.raises(ValueError):
        project_assets.AgentRuntimeAssessmentItemResponse(
            agent_asset_id=_READY_AGENT_ID,
            selected_version_id=selected_version_id,
            status=status,
            reason_code=reason_code,
        )


def _app() -> FastAPI:
    application = FastAPI()
    application.dependency_overrides[project_assets.project_asset_context] = _context
    application.dependency_overrides[project_assets.get_agent_runtime_assessment_service] = _HttpService
    application.include_router(project_assets.project_router)
    return application


@pytest.mark.asyncio
async def test_runtime_assessment_http_contract_is_strict_and_ordered() -> None:
    application = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/agents/runtime-assessments",
            json={
                "agent_ids": [str(_MODEL_AGENT_ID), str(_READY_AGENT_ID)],
            },
        )
        invalid = await client.post(
            f"/api/projects/{_PROJECT_ID}/agents/runtime-assessments",
            json={"agent_ids": [str(_READY_AGENT_ID)], "project_id": str(_PROJECT_ID)},
        )
        duplicate = await client.post(
            f"/api/projects/{_PROJECT_ID}/agents/runtime-assessments",
            json={
                "agent_ids": [str(_READY_AGENT_ID), str(_READY_AGENT_ID)],
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "agent_asset_id": str(_MODEL_AGENT_ID),
                "selected_version_id": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"http-assessment:{_MODEL_AGENT_ID}",
                    )
                ),
                "status": "ready",
                "reason_code": None,
            },
            {
                "agent_asset_id": str(_READY_AGENT_ID),
                "selected_version_id": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"http-assessment:{_READY_AGENT_ID}",
                    )
                ),
                "status": "ready",
                "reason_code": None,
            },
        ],
        "request_id": _REQUEST_ID,
    }
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "asset_validation_failed"
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == "asset_validation_failed"


@pytest.mark.asyncio
async def test_runtime_assessment_http_dependency_requires_endpoint_policy() -> None:
    application = FastAPI()
    application.dependency_overrides[project_assets.project_asset_context] = _context
    application.include_router(project_assets.project_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/agents/runtime-assessments",
            json={"agent_ids": [str(_READY_AGENT_ID)]},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "asset_storage_unavailable"
