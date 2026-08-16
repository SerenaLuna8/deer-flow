from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.audit.models import resolve_system_audit_context
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
    TitlePolicy,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService


class _Session:
    def __init__(self, model_id: uuid.UUID) -> None:
        self.model_id = model_id
        self.statements: list[object] = []

    async def execute(self, statement):  # type: ignore[no-untyped-def]
        self.statements.append(statement)
        return SimpleNamespace(
            all=lambda: (
                SimpleNamespace(
                    id=self.model_id,
                    provider_adapter="openai",
                    settings={},
                    supports_vision=False,
                ),
            )
        )

    async def flush(self) -> None:
        return None


class _Audit:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def append(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.calls.append((*args, kwargs))


class _Repository:
    def __init__(self, model_id: uuid.UUID) -> None:
        self.session = _Session(model_id)
        self.state = SimpleNamespace(
            revision=4,
            updated_by_user_id=None,
            updated_at=None,
        )
        self.policy = SimpleNamespace(
            revision=1,
            updated_by_user_id=None,
            updated_at=None,
        )
        self.previous = SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000402"),
        )
        self.version = None

    async def catalog_state(self, *, for_update: bool = False):
        assert for_update is True
        return self.state

    async def current(self, section, *, for_update: bool = False):  # type: ignore[no-untyped-def]
        assert section is RuntimePolicySection.AGENT_RUNTIME
        assert for_update is True
        return self.policy, self.previous

    async def add_version(self, policy, version) -> None:  # type: ignore[no-untyped-def]
        assert policy is self.policy
        self.version = version


class _Service(SystemRuntimePolicyService):
    def __init__(self, repository: _Repository, audit: _Audit) -> None:
        super().__init__(lambda: None, audit)  # type: ignore[arg-type]
        self.repository = repository

    async def _admin_operation(self, context, operation):  # type: ignore[no-untyped-def]
        return await operation(self.repository, self._require_admin(context))


def _admin_context():
    return resolve_system_audit_context(
        SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000401"),
            system_role="system_admin",
        ),
        request_id="system-runtime-model-refs",
    )


@pytest.mark.anyio
async def test_runtime_policy_locks_models_by_uuid_primary_key() -> None:
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000403")
    repository = _Repository(model_id)
    audit = _Audit()

    result = await _Service(repository, audit).update_policy(
        _admin_context(),
        RuntimePolicySection.AGENT_RUNTIME,
        expected_revision=1,
        value=AgentRuntimePolicyValue(
            title=TitlePolicy(model_name=str(model_id)),
        ),
    )

    assert result.policy.value.title.model_name == str(model_id)
    assert repository.version is not None
    assert len(repository.session.statements) == 1
    statement = repository.session.statements[0]
    sql = " ".join(str(statement).split())
    assert "system_model_configs.id IN" in sql
    assert "logical_name" not in sql
    params = statement.compile().params
    assert any(model_id in value for value in params.values() if isinstance(value, list))
    assert len(audit.calls) == 1
