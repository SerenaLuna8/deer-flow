from __future__ import annotations

import uuid
from types import MethodType, SimpleNamespace

import pytest

from app.private_work import chat_controls as chat_controls_module
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.agents.middlewares.provider_request_usage import (
    provider_request_runtime_policy_identity,
)
from deerflow.runtime.context_compaction import ContextUsageUnsupported

_SELECTED_MODEL = "11111111-1111-4111-8111-111111111111"
_SUMMARY_MODEL = "22222222-2222-4222-8222-222222222222"
_FROZEN_LEAD_MODEL = "33333333-3333-4333-8333-333333333333"
_FROZEN_SUMMARY_MODEL = "44444444-4444-4444-8444-444444444444"
_TITLE_MODEL = "55555555-5555-4555-8555-555555555555"


def _context() -> PrivateWorkContext:
    role = ProjectRole.ADMIN
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="context-usage-service",
        )
    )


class _IdlePolicyConfig:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return {"tool_search": {"marker": self.marker}}


def test_idle_context_usage_reuses_only_the_exact_frozen_profile() -> None:
    config = _IdlePolicyConfig("same")
    profile = {
        "model_name": _SELECTED_MODEL,
        "closure_identity": "closure-1",
        "runtime_policy_identity": provider_request_runtime_policy_identity(config),
        "workload_profile": "interactive",
        "mcp_closure_present": False,
    }
    snapshot = SimpleNamespace(values={"provider_request_profile": profile})

    assert (
        ProjectChatControlService._idle_provider_request_profile(
            snapshot,
            runtime_config=config,  # type: ignore[arg-type]
            authority=SimpleNamespace(
                run_id=None,
                lead_model_ref=_SELECTED_MODEL,
                closure_identity="closure-1",
            ),
        )
        is profile
    )


@pytest.mark.parametrize(
    "profile_update",
    (
        {"model_name": _FROZEN_LEAD_MODEL},
        {"closure_identity": "closure-2"},
        {"runtime_policy_identity": "stale"},
        {"workload_profile": "research"},
        {"mcp_closure_present": True},
    ),
)
def test_idle_context_usage_fails_closed_on_request_shape_drift(
    profile_update: dict[str, object],
) -> None:
    config = _IdlePolicyConfig("same")
    profile = {
        "model_name": _SELECTED_MODEL,
        "closure_identity": "closure-1",
        "runtime_policy_identity": provider_request_runtime_policy_identity(config),
        "workload_profile": "interactive",
        "mcp_closure_present": False,
        **profile_update,
    }

    with pytest.raises(ContextUsageUnsupported):
        ProjectChatControlService._idle_provider_request_profile(
            SimpleNamespace(values={"provider_request_profile": profile}),
            runtime_config=config,  # type: ignore[arg-type]
            authority=SimpleNamespace(
                run_id=None,
                lead_model_ref=_SELECTED_MODEL,
                closure_identity="closure-1",
            ),
        )


@pytest.mark.asyncio
async def test_context_usage_reuses_compact_authority_without_blocking_active_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProjectChatControlService)
    events: list[object] = []
    source_config = object()
    runtime_config = object()
    snapshot = SimpleNamespace(
        values={"messages": []},
        config={"configurable": {"checkpoint_id": "checkpoint-1"}},
    )
    expected = object()

    async def authority(
        _service,
        _context,
        thread_id: str,
        *,
        selected_model_name: str | None,
    ):
        events.append(("authority", thread_id, selected_model_name))
        return SimpleNamespace(run_id="run-1", lead_model_ref=_FROZEN_LEAD_MODEL)

    async def materialize(
        _service,
        _context,
        app_config,
        *,
        authority,
        selected_model_name,
    ):
        events.append(("materialize", app_config, authority, selected_model_name))
        return runtime_config, _FROZEN_LEAD_MODEL

    class _State:
        async def aget(self, config):
            events.append(("read", config))
            return snapshot

    def state(_service, _context, app_config, *, as_node: str):
        events.append(("state", app_config, as_node))
        return _State()

    monkeypatch.setattr(
        service,
        "_resolve_context_usage_authority",
        MethodType(authority, service),
    )
    monkeypatch.setattr(
        service,
        "_materialize_context_usage_config",
        MethodType(materialize, service),
    )
    monkeypatch.setattr(service, "_state", MethodType(state, service))
    monkeypatch.setattr(
        chat_controls_module,
        "measure_thread_context_usage",
        lambda actual_snapshot, *, app_config, context_model_name, provider_request_profile, expected_authority_identity, require_provider_request_profile: (
            events.append(("measure", actual_snapshot, app_config, context_model_name, expected_authority_identity)) or expected
        ),
        raising=False,
    )

    result = await service.context_usage(
        _context(),
        "thread-1",
        app_config=source_config,
    )

    assert result is expected
    assert events == [
        ("authority", "thread-1", None),
        (
            "materialize",
            source_config,
            SimpleNamespace(run_id="run-1", lead_model_ref=_FROZEN_LEAD_MODEL),
            None,
        ),
        ("state", runtime_config, "context_usage"),
        (
            "read",
            {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "",
                }
            },
        ),
        ("authority", "thread-1", None),
        ("measure", snapshot, runtime_config, _FROZEN_LEAD_MODEL, "run-1"),
    ]


@pytest.mark.asyncio
async def test_context_usage_materializes_the_composer_selected_lead_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProjectChatControlService)
    materialized: list[object] = []
    snapshot = SimpleNamespace(
        values={"messages": []},
        config={"configurable": {"checkpoint_id": "checkpoint-1"}},
    )

    class _TitleConfig:
        enabled = True
        model_name = None

        def model_copy(self, *, update):
            materialized.append(("title_copy", update["model_name"]))
            return SimpleNamespace(
                enabled=self.enabled,
                model_name=update["model_name"],
            )

    class _Config:
        summarization = SimpleNamespace(model_name="default")
        title = _TitleConfig()

        def with_runtime_models(self, models):
            materialized.append(("runtime_models", tuple(model.name for model in models)))
            return self

        def model_copy(self, *, update):
            materialized.append(("config_copy", update["title"].model_name))
            self.title = update["title"]
            return self

    config = _Config()

    class _ModelMaterializer:
        async def materialize_active(self, model_ref):
            materialized.append(("active", model_ref))
            if model_ref is None:
                return SimpleNamespace(name=_TITLE_MODEL)
            return SimpleNamespace(
                name=_SUMMARY_MODEL if model_ref == "default" else model_ref,
            )

        async def materialize_snapshot(self, **kwargs):
            raise AssertionError(f"unexpected snapshot materialization: {kwargs}")

    service._model_materializer = _ModelMaterializer()
    service._runtime_policy_materializer = SimpleNamespace()

    async def authority(_service, _context, thread_id, *, selected_model_name):
        materialized.append(("authority", thread_id, selected_model_name))
        return SimpleNamespace(
            run_id=None,
            lead_model_ref=selected_model_name,
        )

    class _State:
        async def aget(self, _config):
            return snapshot

    monkeypatch.setattr(
        service,
        "_resolve_context_usage_authority",
        MethodType(authority, service),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_state",
        MethodType(
            lambda _service, _context, app_config, *, as_node: materialized.append(("state", app_config, as_node)) or _State(),
            service,
        ),
    )
    monkeypatch.setattr(
        service,
        "_idle_provider_request_profile",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        chat_controls_module,
        "measure_thread_context_usage",
        lambda actual_snapshot, *, app_config, context_model_name, provider_request_profile, expected_authority_identity, require_provider_request_profile: (
            materialized.append(("measure", actual_snapshot, app_config, context_model_name, expected_authority_identity)) or "usage"
        ),
    )

    result = await service.context_usage(
        _context(),
        "thread-1",
        app_config=config,  # type: ignore[arg-type]
        selected_model_name=_SELECTED_MODEL,
    )

    assert result == "usage"
    assert materialized == [
        ("authority", "thread-1", _SELECTED_MODEL),
        ("active", _SELECTED_MODEL),
        ("active", "default"),
        ("active", None),
        (
            "runtime_models",
            (_SELECTED_MODEL, _SUMMARY_MODEL, _TITLE_MODEL),
        ),
        ("title_copy", _TITLE_MODEL),
        ("config_copy", _TITLE_MODEL),
        ("state", config, "context_usage"),
        ("authority", "thread-1", _SELECTED_MODEL),
        ("measure", snapshot, config, _SELECTED_MODEL, None),
    ]


@pytest.mark.asyncio
async def test_context_usage_uses_active_run_frozen_policy_and_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProjectChatControlService)
    materialized: list[object] = []
    context = _context()
    snapshot = SimpleNamespace(
        values={"messages": []},
        config={"configurable": {"checkpoint_id": "checkpoint-1"}},
    )
    frozen_policy = SimpleNamespace(
        summarization=SimpleNamespace(model_name=_FROZEN_SUMMARY_MODEL),
    )

    class _Config:
        summarization = SimpleNamespace(model_name=_SUMMARY_MODEL)

        def with_runtime_policy(self, policy):
            materialized.append(("runtime_policy", policy))
            runtime = _Config()
            runtime.summarization = frozen_policy.summarization
            return runtime

        def with_runtime_models(self, models):
            materialized.append(("runtime_models", tuple(model.name for model in models)))
            return self

    config = _Config()

    class _PolicyMaterializer:
        async def materialize_run_snapshot_envelope(self, **kwargs):
            materialized.append(("policy_snapshot", kwargs))
            return SimpleNamespace(value=frozen_policy)

    class _ModelMaterializer:
        async def materialize_active(self, model_ref):
            raise AssertionError(f"current model must not be used: {model_ref}")

        async def materialize_snapshot(self, **kwargs):
            materialized.append(("model_snapshot", kwargs))
            name = _FROZEN_LEAD_MODEL if kwargs["purpose"] == "lead" else _FROZEN_SUMMARY_MODEL
            return SimpleNamespace(name=name)

    service._model_materializer = _ModelMaterializer()
    service._runtime_policy_materializer = _PolicyMaterializer()

    async def authority(_service, _context, thread_id, *, selected_model_name):
        materialized.append(("authority", thread_id, selected_model_name))
        return SimpleNamespace(
            run_id="run-1",
            lead_model_ref=_FROZEN_LEAD_MODEL,
        )

    class _State:
        async def aget(self, _config):
            return snapshot

    monkeypatch.setattr(
        service,
        "_resolve_context_usage_authority",
        MethodType(authority, service),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_state",
        MethodType(
            lambda _service, _context, app_config, *, as_node: materialized.append(("state", app_config, as_node)) or _State(),
            service,
        ),
    )
    monkeypatch.setattr(
        service,
        "_idle_provider_request_profile",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        chat_controls_module,
        "project_memory_compaction_app_config_policy",
        lambda policy: ("projected", policy),
        raising=False,
    )
    monkeypatch.setattr(
        chat_controls_module,
        "measure_thread_context_usage",
        lambda actual_snapshot, *, app_config, context_model_name, provider_request_profile, expected_authority_identity, require_provider_request_profile: (
            materialized.append(("measure", actual_snapshot, app_config, context_model_name, expected_authority_identity)) or "usage"
        ),
    )

    result = await service.context_usage(
        context,
        "thread-1",
        app_config=config,  # type: ignore[arg-type]
        selected_model_name=_SELECTED_MODEL,
    )

    assert result == "usage"
    assert materialized[0] == ("authority", "thread-1", _SELECTED_MODEL)
    assert materialized[1] == (
        "policy_snapshot",
        {
            "project_id": context.project_id,
            "owner_user_id": str(context.user_id),
            "run_id": "run-1",
        },
    )
    assert materialized[2] == ("runtime_policy", ("projected", frozen_policy))
    assert [entry[0] for entry in materialized].count("model_snapshot") == 2
    assert ("runtime_models", (_FROZEN_LEAD_MODEL, _FROZEN_SUMMARY_MODEL)) in materialized
    assert materialized[-1][0] == "measure"
    assert materialized[-1][3] == _FROZEN_LEAD_MODEL


@pytest.mark.parametrize(
    ("initial", "settled"),
    (
        (
            SimpleNamespace(run_id=None, lead_model_ref=_SELECTED_MODEL),
            SimpleNamespace(run_id="run-2", lead_model_ref=_FROZEN_LEAD_MODEL),
        ),
        (
            SimpleNamespace(run_id="run-2", lead_model_ref=_FROZEN_LEAD_MODEL),
            SimpleNamespace(run_id=None, lead_model_ref=_SELECTED_MODEL),
        ),
    ),
    ids=("run-admitted", "run-terminal"),
)
@pytest.mark.asyncio
async def test_context_usage_recomputes_the_whole_read_when_run_authority_changes_mid_measurement(
    monkeypatch: pytest.MonkeyPatch,
    initial: SimpleNamespace,
    settled: SimpleNamespace,
) -> None:
    service = object.__new__(ProjectChatControlService)
    authorities = iter((initial, settled, settled))
    events: list[object] = []

    async def authority(
        _service,
        _context,
        thread_id: str,
        *,
        selected_model_name: str | None,
    ):
        resolved = next(authorities)
        events.append(("authority", thread_id, selected_model_name, resolved))
        return resolved

    async def materialize(
        _service,
        _context,
        _app_config,
        *,
        authority,
        selected_model_name,
    ):
        events.append(("materialize", authority, selected_model_name))
        return authority, authority.lead_model_ref

    class _State:
        def __init__(self, measured_authority):
            self._measured_authority = measured_authority

        async def aget(self, _config):
            events.append(("read", self._measured_authority))
            return SimpleNamespace(
                values={"measured_authority": self._measured_authority},
                config={"configurable": {"checkpoint_id": "checkpoint-1"}},
            )

    monkeypatch.setattr(
        service,
        "_resolve_context_usage_authority",
        MethodType(authority, service),
    )
    monkeypatch.setattr(
        service,
        "_materialize_context_usage_config",
        MethodType(materialize, service),
    )
    monkeypatch.setattr(
        service,
        "_state",
        MethodType(
            lambda _service, _context, runtime_config, *, as_node: events.append(("state", runtime_config, as_node)) or _State(runtime_config),
            service,
        ),
    )
    monkeypatch.setattr(
        service,
        "_idle_provider_request_profile",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        chat_controls_module,
        "measure_thread_context_usage",
        lambda snapshot, *, app_config, context_model_name, provider_request_profile, expected_authority_identity, require_provider_request_profile: (
            events.append(("measure", snapshot.values["measured_authority"], app_config, context_model_name, expected_authority_identity)) or snapshot.values["measured_authority"]
        ),
    )

    result = await service.context_usage(
        _context(),
        "thread-1",
        app_config=object(),  # type: ignore[arg-type]
        selected_model_name=_SELECTED_MODEL,
    )

    assert result is settled
    assert [event[1] for event in events if event[0] == "materialize"] == [
        initial,
        settled,
    ]
    assert [event[1] for event in events if event[0] == "read"] == [
        initial,
        settled,
    ]
    assert [event[1] for event in events if event[0] == "measure"] == [settled]


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


class _AuthoritySession:
    def __init__(self, active_run):
        self.active_run = active_run

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return _Transaction()

    async def execute(self, _statement):
        return SimpleNamespace(one_or_none=lambda: self.active_run)


class _MarkerSession(_AuthoritySession):
    def __init__(self, marker_rows: tuple[str | None, str | None]):
        super().__init__(None)
        self.marker_rows = marker_rows

    async def execute(self, _statement):
        return SimpleNamespace(one=lambda: self.marker_rows)


@pytest.mark.parametrize(
    ("active_run_id", "latest_run_id", "expected"),
    (
        ("run-oldest-active", "run-latest", "active:run-oldest-active"),
        (None, "run-latest", "idle:run-latest"),
        (None, None, "idle:none"),
    ),
)
@pytest.mark.asyncio
async def test_context_usage_authority_marker_projects_only_run_identity(
    monkeypatch: pytest.MonkeyPatch,
    active_run_id: str | None,
    latest_run_id: str | None,
    expected: str,
) -> None:
    context = _context()
    service = object.__new__(ProjectChatControlService)
    required: list[tuple[object, ...]] = []
    service._session_factory = lambda: _MarkerSession((active_run_id, latest_run_id))
    service._revalidator = SimpleNamespace(
        require=lambda *_args, **_kwargs: (
            required.append((*_args[2:], _kwargs)),
            _async_value(object()),
        )[1]
    )
    service._model_materializer = SimpleNamespace(materialize_active=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("marker must not materialize models")))
    service._runtime_policy_materializer = SimpleNamespace(materialize_run_snapshot_envelope=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("marker must not materialize policy")))

    class _Threads:
        async def get(self, **_kwargs):
            return SimpleNamespace()

    monkeypatch.setattr(
        chat_controls_module,
        "PrivateThreadRepository",
        lambda _session: _Threads(),
    )

    marker = await service.context_usage_authority_marker(context, "thread-1")

    assert marker.cache_marker == expected
    assert required == [
        (
            chat_controls_module.Capability.PRIVATE_WORK_CREATE,
            chat_controls_module.Capability.SHARED_ASSETS_EXECUTE,
            {"lock": False},
        )
    ]


@pytest.mark.asyncio
async def test_context_usage_authority_prefers_an_active_run_over_composer_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    service = object.__new__(ProjectChatControlService)
    service._session_factory = lambda: _AuthoritySession(("run-1", _FROZEN_LEAD_MODEL))
    service._revalidator = SimpleNamespace(require=lambda *_args, **_kwargs: _async_value(object()))
    service._resolver = SimpleNamespace(resolve_project_asset_snapshot_in_session=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("active Run must bypass current Agent resolution")))
    service._snapshots = SimpleNamespace()

    class _Threads:
        async def get(self, **_kwargs):
            return SimpleNamespace()

    monkeypatch.setattr(
        chat_controls_module,
        "PrivateThreadRepository",
        lambda _session: _Threads(),
    )

    authority = await service._resolve_context_usage_authority(
        context,
        "thread-1",
        selected_model_name=_SELECTED_MODEL,
    )

    assert authority.run_id == "run-1"
    assert authority.lead_model_ref == _FROZEN_LEAD_MODEL


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_context_usage_authority_uses_composer_selection_without_active_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    service = object.__new__(ProjectChatControlService)
    service._session_factory = lambda: _AuthoritySession(None)
    service._revalidator = SimpleNamespace(require=lambda *_args, **_kwargs: _async_value(object()))

    class _Resolved:
        scope = SimpleNamespace(value="project")
        payload = SimpleNamespace(model_ref="default")
        version_id = uuid.uuid4()
        checksum = "a" * 64
        catalog_generation = 7

    class _Resolver:
        async def resolve_project_asset_snapshot_in_session(self, *_args, **_kwargs):
            return _Resolved()

    class _Snapshots:
        async def validate_agent_closure_in_session(self, *_args, **_kwargs):
            return None

    class _Threads:
        async def get(self, **_kwargs):
            return SimpleNamespace(
                agent_asset_id=uuid.uuid4(),
                agent_scope="project",
            )

    service._resolver = _Resolver()
    service._snapshots = _Snapshots()
    monkeypatch.setattr(chat_controls_module, "ResolvedAgentSnapshot", _Resolved)
    monkeypatch.setattr(
        chat_controls_module,
        "PrivateThreadRepository",
        lambda _session: _Threads(),
    )

    authority = await service._resolve_context_usage_authority(
        context,
        "thread-1",
        selected_model_name=_SELECTED_MODEL,
    )

    assert authority.run_id is None
    assert authority.lead_model_ref == _SELECTED_MODEL
