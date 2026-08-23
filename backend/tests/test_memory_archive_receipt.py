from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.run_admission import (
    _strip_client_memory_archive_receipt,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_RECEIPT_KEY,
    SNIP_ARCHIVE_PROMPT_VERSION,
    compute_snip_content_digest,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.model_execution import SystemModelExecutionProvenance
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentScope,
    MemoryHistoryActivation,
)
from deerflow.runtime.serialization import serialize, serialize_channel_values_for_api


class _FakeTransaction:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTransaction:
        self._session.transaction_active = True
        self._session.events.append("transaction")
        return self

    async def __aexit__(self, *_args: object) -> None:
        self._session.transaction_active = False


class _FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.transaction_active = False

    async def __aenter__(self) -> _FakeSession:
        self.events.append("session")
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def in_transaction(self) -> bool:
        return self.transaction_active


class _CheckpointAuthorizationBoundary:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def before_checkpoint_read(self) -> None:
        self._events.append("boundary")


def _private_context() -> PrivateWorkContext:
    role = ProjectRole.ADMIN
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="memory-receipt-lock-order",
        )
    )


def _activation(**overrides: object) -> MemoryHistoryActivation:
    tagged_text = "- [durable] The checkpoint handshake is idempotent"
    values: dict[str, object] = {
        "scope": MemoryDocumentScope(uuid.uuid4(), str(uuid.uuid4())),
        "thread_id": "thread-1",
        "source_checkpoint_id": "source-1",
        "committed_checkpoint_id": "committed-1",
        "source_digest": "a" * 64,
        "tagged_text": tagged_text,
        "content_digest": compute_snip_content_digest(tagged_text),
        "preference_version": 1,
        "snip_prompt_version": SNIP_ARCHIVE_PROMPT_VERSION,
        "summary_model": SystemModelExecutionProvenance(
            model_config_id=uuid.uuid4(),
            payload_checksum="b" * 64,
            secret_generation_id=uuid.uuid4(),
            secret_envelope_digest="c" * 64,
        ),
    }
    values.update(overrides)
    return MemoryHistoryActivation(**values)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_worker_authorization_boundary_keeps_scope_revalidation_in_checkpoint_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = _FakeSession(events)
    scoped = ProjectScopedCheckpointer(
        InMemorySaver(),
        lambda: session,  # type: ignore[arg-type]
    ).for_context(_private_context())
    scoped.set_authorization_boundary(_CheckpointAuthorizationBoundary(events))

    async def require_scope(
        checked_session: object,
        _context: object,
        capability: Capability,
        *,
        lock: bool,
    ) -> None:
        assert checked_session is session
        assert session.in_transaction()
        assert capability is Capability.PRIVATE_WORK_READ_OWN
        assert lock is True
        events.append("scope")

    async def get_thread(
        _repository: PrivateThreadRepository,
        *,
        scope: object,
        thread_id: str,
        lock: bool,
        thread_kind: str,
    ) -> object:
        del scope
        assert session.in_transaction()
        assert thread_id == "thread-1"
        assert lock is True
        assert thread_kind == "chat"
        events.append("thread")
        return object()

    monkeypatch.setattr(scoped._revalidator, "require", require_scope)
    monkeypatch.setattr(PrivateThreadRepository, "get", get_thread)

    result = await scoped.aget_tuple({"configurable": {"thread_id": "thread-1"}})

    assert result is None
    assert events == ["boundary", "session", "transaction", "scope", "thread"]


def test_history_activation_contract_rejects_non_snip_and_digest_drift() -> None:
    assert _activation().tagged_text.startswith("- [durable]")

    with pytest.raises(ValueError):
        _activation(tagged_text="(nothing)")
    with pytest.raises(ValueError):
        _activation(content_digest="b" * 64)
    with pytest.raises(ValueError):
        _activation(source_digest="A" * 64)


def test_public_serialization_recursively_redacts_archive_receipts() -> None:
    receipt = {"tagged_text": "private history"}
    payload = {
        MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
        "messages": [
            {
                "content": "visible",
                "additional_kwargs": {
                    MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
                },
            }
        ],
        "nested": {
            "updates": {
                MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
                "summary_text": "visible summary",
            }
        },
    }

    values = serialize(payload, mode="values")
    updates = serialize({"checkpoint": payload}, mode="updates")
    rest = serialize_channel_values_for_api(payload)

    assert MEMORY_ARCHIVE_RECEIPT_KEY not in values
    assert MEMORY_ARCHIVE_RECEIPT_KEY not in values["messages"][0]["additional_kwargs"]
    assert MEMORY_ARCHIVE_RECEIPT_KEY not in values["nested"]["updates"]
    assert MEMORY_ARCHIVE_RECEIPT_KEY not in updates["checkpoint"]
    assert MEMORY_ARCHIVE_RECEIPT_KEY not in rest


def test_client_receipt_strip_preserves_message_and_tool_data() -> None:
    input_payload = {
        MEMORY_ARCHIVE_RECEIPT_KEY: {"forged": True},
        "messages": [
            {
                "content": "hello",
                "additional_kwargs": {
                    MEMORY_ARCHIVE_RECEIPT_KEY: "ordinary message data",
                },
            }
        ],
    }
    command_payload = {
        "resume": "continue",
        "update": {
            MEMORY_ARCHIVE_RECEIPT_KEY: {"forged": True},
            "messages": input_payload["messages"],
        },
    }

    clean_input = _strip_client_memory_archive_receipt(
        input_payload,
        command=False,
    )
    clean_command = _strip_client_memory_archive_receipt(
        command_payload,
        command=True,
    )

    assert isinstance(clean_input, dict)
    assert MEMORY_ARCHIVE_RECEIPT_KEY not in clean_input
    assert clean_input["messages"][0]["additional_kwargs"][MEMORY_ARCHIVE_RECEIPT_KEY] == "ordinary message data"
    assert isinstance(clean_command, dict)
    assert MEMORY_ARCHIVE_RECEIPT_KEY not in clean_command["update"]
    assert clean_command["update"]["messages"] == input_payload["messages"]


def test_exact_model_version_carrier_survives_runtime_copy_and_is_secret() -> None:
    exact_version = uuid.uuid4()
    model = ModelConfig(
        name="summary-model",
        display_name=None,
        description=None,
        use="langchain_openai.ChatOpenAI",
        model="summary-model",
        max_input_tokens=64_000,
    )
    model._system_model_config_id = exact_version
    config = AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            }
        }
    ).with_runtime_models((model,))

    assert config.models[0]._system_model_config_id == exact_version
    assert "_system_model_config_id" not in config.models[0].model_dump()
    assert "_system_model_config_id" not in config.model_dump()
