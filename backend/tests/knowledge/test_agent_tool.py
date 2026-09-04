"""M6 — ``knowledge_search`` Agent tool and lead-agent factory wrapper.

Covers the Worker-side adapter in ``app.knowledge.run_tool``:

- tool contract: model-visible arguments, Project binding, items JSON content,
  complete Knowledge Citations in ``additional_kwargs``, error ToolMessages
  without citation payloads;
- tool/HTTP consistency: the tool's citation payload matches the search API's
  response model for the same ``KnowledgeCitation``;
- factory wrapper: injects the tool into the real private lead-agent graph
  assembly, preserves the canonical system prompt, refuses caller-supplied
  trusted extensions, and keeps the keyword signature the Worker probes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_SEARCH_FAILED,
    KnowledgeBaseFilterFields,
    KnowledgeCitation,
    KnowledgeError,
    KnowledgeFilterFieldView,
    KnowledgeSearchHit,
    KnowledgeSearchResult,
)
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import SecretStr

import deerflow.agents.lead_agent.agent as lead_agent_module
from app.knowledge.run_tool import (
    KNOWLEDGE_CITATIONS_KEY,
    KNOWLEDGE_METADATA_FIELDS_TOOL_NAME,
    KNOWLEDGE_SEARCH_TOOL_NAME,
    create_knowledge_lead_agent_factory,
    create_knowledge_metadata_fields_tool,
    create_knowledge_search_tool,
)
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.agents.lead_agent.agent import TrustedLeadAgentExtension
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig

PROJECT_ID = uuid.UUID("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")
OWNER_USER_ID = uuid.UUID("dddddddd-4444-4444-8444-dddddddddddd")
MODEL_NAME = "88888888-8888-4888-8888-888888888888"


class _FakeAuthority:
    project_id = PROJECT_ID
    actor_user_id = OWNER_USER_ID

    async def revalidate(self, session) -> None:  # noqa: ANN001
        del session


_AUTHORITY = _FakeAuthority()


def _citation(*, position: int = 1, score: float = 0.91) -> KnowledgeCitation:
    snippet = f"第 {position} 段:量子比特与叠加态……"
    return KnowledgeCitation(
        knowledge_base_id=uuid.UUID("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"),
        knowledge_base_name="产品手册",
        document_id=uuid.UUID("cccccccc-3333-4333-8333-cccccccccccc"),
        document_name="发布说明.pdf",
        segment_id=uuid.uuid5(uuid.NAMESPACE_URL, f"segment-{position}"),
        segment_position=position,
        snippet=snippet,
        score=score,
        source_position={"page": position},
        document_version=1,
        content_digest=hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
        score_kind="cosine",
    )


def _hit(citation: KnowledgeCitation, passage: str | None = None, *, model_text: str | None = None) -> KnowledgeSearchHit:
    """Wrap a citation the way the retrieval service does since T1."""
    full_passage = passage if passage is not None else citation.snippet
    return KnowledgeSearchHit(
        citation=citation,
        passage=full_passage,
        model_text=model_text if model_text is not None else full_passage,
        document_version=1,
        content_digest=hashlib.sha256(full_passage.encode("utf-8")).hexdigest(),
        local_score=citation.score,
        local_score_kind="cosine",
        score_domain="embedding:test",
        ranking_method="cosine",
        ranking_score=citation.score,
    )


class _FakeKnowledgeModule:
    """Duck-typed KnowledgeModule capturing search requests."""

    def __init__(
        self,
        *,
        result: KnowledgeSearchResult | None = None,
        error: KnowledgeError | None = None,
    ) -> None:
        self.requests: list[object] = []
        self.authorities: list[object] = []
        self._result = result or KnowledgeSearchResult()
        self._error = error

    async def search(self, request, *, authority):  # noqa: ANN001
        self.requests.append(request)
        self.authorities.append(authority)
        if self._error is not None:
            raise self._error
        return self._result


def _search_tool(module: _FakeKnowledgeModule):
    return create_knowledge_search_tool(
        module,
        PROJECT_ID,
        OWNER_USER_ID,
        _AUTHORITY,
    )


async def _invoke(
    tool_obj,  # noqa: ANN001
    *,
    query: str,
    top_k: int | None = None,
    metadata_filters: list[object] | None = None,
    knowledge_base_ids: list[object] | None = None,
    call_id: str = "call-1",
):
    """Invoke through the ToolCall form so InjectedToolCallId is exercised."""
    args: dict[str, object] = {"query": query}
    if top_k is not None:
        args["top_k"] = top_k
    if metadata_filters is not None:
        args["metadata_filters"] = metadata_filters
    if knowledge_base_ids is not None:
        args["knowledge_base_ids"] = knowledge_base_ids
    return await tool_obj.ainvoke(
        {
            "type": "tool_call",
            "name": KNOWLEDGE_SEARCH_TOOL_NAME,
            "id": call_id,
            "args": args,
        }
    )


def _tool_message(command: Command) -> ToolMessage:
    assert type(command) is Command
    messages = command.update["messages"]
    assert len(messages) == 1
    message = messages[0]
    assert type(message) is ToolMessage
    return message


# ---------------------------------------------------------------------------
# Tool contract
# ---------------------------------------------------------------------------


class TestKnowledgeSearchTool:
    @pytest.mark.asyncio
    async def test_trusted_owner_is_bound_in_the_tool_closure_not_model_arguments(self) -> None:
        module = _FakeKnowledgeModule()
        tool_obj = _search_tool(module)

        await _invoke(tool_obj, query="私有 Run 查询")

        assert module.requests[0].owner_user_id == OWNER_USER_ID
        assert module.requests[0].project_id == PROJECT_ID
        assert "project_id" not in tool_obj.tool_call_schema.model_fields
        assert "owner_user_id" not in tool_obj.tool_call_schema.model_fields
        assert "owner_user_id" not in json.dumps(tool_obj.args)

    @pytest.mark.asyncio
    async def test_knowledge_base_ids_scope_the_search_and_reject_bad_uuids(self) -> None:
        """The model may narrow a search to bases it discovered; scope stays a
        subset of the project the closure bound, never a project switch."""

        module = _FakeKnowledgeModule(result=KnowledgeSearchResult(hits=(_hit(_citation()),)))
        tool_obj = _search_tool(module)
        base_id = uuid.UUID("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")

        message = _tool_message(await _invoke(tool_obj, query="安装", knowledge_base_ids=[str(base_id)]))
        assert message.status != "error"
        assert module.requests[0].knowledge_base_ids == (base_id,)
        assert module.requests[0].project_id == PROJECT_ID

        error = _tool_message(await _invoke(tool_obj, query="安装", knowledge_base_ids=["不是UUID"]))
        assert error.status == "error"
        assert KNOWLEDGE_INVALID_REQUEST in error.content
        assert len(module.requests) == 1

    @pytest.mark.asyncio
    async def test_success_returns_items_json_and_full_citations(self) -> None:
        first = _citation(position=1, score=0.93)
        second = _citation(position=2, score=-0.37)
        module = _FakeKnowledgeModule(result=KnowledgeSearchResult(hits=(_hit(first), _hit(second))))
        tool_obj = _search_tool(module)

        command = await _invoke(tool_obj, query="量子计算的原理", top_k=6, call_id="call-9")

        request = module.requests[0]
        assert request.project_id == PROJECT_ID
        assert request.query == "量子计算的原理"
        assert request.top_k == 6
        assert request.score_threshold is None
        assert request.knowledge_base_ids is None

        message = _tool_message(command)
        assert message.tool_call_id == "call-9"
        assert message.status != "error"

        body = json.loads(message.content)
        assert body["delivered_count"] == 2
        assert body["omitted_count"] == 0
        assert body["context_limited"] is False
        items = body["items"]
        assert [set(item) for item in items] == [
            {"knowledge_base_name", "document_name", "passage", "score", "source_position"},
        ] * 2
        assert items[0]["passage"] == first.snippet
        assert items[0]["score"] == first.score
        assert items[1]["score"] == -0.37
        assert items[1]["source_position"] == {"page": 2}

        citations = message.additional_kwargs[KNOWLEDGE_CITATIONS_KEY]
        assert [item["segment_id"] for item in citations] == [
            str(first.segment_id),
            str(second.segment_id),
        ]
        assert citations[0]["knowledge_base_id"] == str(first.knowledge_base_id)
        assert citations[0]["document_id"] == str(first.document_id)
        assert citations[0]["document_name"] == first.document_name
        assert citations[0]["segment_position"] == 1
        assert citations[0]["score"] == first.score
        assert citations[1]["score"] == -0.37
        # T5 provenance rides along so the browser can locate the exact
        # content version later without re-searching.
        assert citations[0]["document_version"] == first.document_version
        assert citations[0]["content_digest"] == first.content_digest
        assert citations[0]["score_kind"] == "cosine"

    @pytest.mark.asyncio
    async def test_metadata_filter_field_kind_passes_through_and_defaults_to_custom(self) -> None:
        from actweave_knowledge import KnowledgeMetadataFilter

        module = _FakeKnowledgeModule()
        tool_obj = _search_tool(module)

        await _invoke(
            tool_obj,
            query="上月上传的 PDF",
            metadata_filters=[
                {"name": "file_type", "operator": "eq", "value": "pdf", "field_kind": "builtin"},
                {"name": "file_type", "operator": "eq", "value": "规范"},
            ],
        )

        assert module.requests[0].metadata_filters == (
            KnowledgeMetadataFilter(name="file_type", operator="eq", value="pdf", field_kind="builtin"),
            KnowledgeMetadataFilter(name="file_type", operator="eq", value="规范", field_kind="custom"),
        )
        # The argument schema teaches field_kind without inventing new
        # top-level parameters.
        assert "field_kind" in json.dumps(tool_obj.args)

    @pytest.mark.asyncio
    async def test_items_send_the_model_text_not_the_escaped_markdown(self) -> None:
        """PDF/Word text reaches the model without Markdown escapes or image
        refs: the item passage is the persisted ``index_text``, while the
        citation digest and snippet keep describing the stored Markdown."""

        markdown = "字段 user\\_id 与 \\[1\\] 说明 ![图](knowledge-attachment:" + "a" * 64 + ")"
        model_text = "字段 user_id 与 [1] 说明 图"
        citation = _citation(position=1, score=0.9)
        module = _FakeKnowledgeModule(result=KnowledgeSearchResult(hits=(_hit(citation, passage=markdown, model_text=model_text),)))
        tool_obj = _search_tool(module)

        message = _tool_message(await _invoke(tool_obj, query="user_id"))

        body = json.loads(message.content)
        [item] = body["items"]
        assert item["passage"] == model_text
        assert "\\_" not in item["passage"]
        assert "knowledge-attachment:" not in item["passage"]
        # The UI-facing citation is untouched: same short quote and digest.
        [payload] = message.additional_kwargs[KNOWLEDGE_CITATIONS_KEY]
        assert payload["snippet"] == citation.snippet
        assert payload["content_digest"] == citation.content_digest

    @pytest.mark.asyncio
    async def test_budget_skips_oversized_passages_and_reports_omissions(self) -> None:
        """Whole-passage packing: an unfittable item is skipped, later ones still try."""

        big = "甲" * 12_000  # ~36 KiB UTF-8
        huge = "乙" * 12_000  # would overflow together with ``big``
        small = "丙" * 2_000  # ~6 KiB fits after the skip
        hits = tuple(_hit(_citation(position=position, score=1.0 - position * 0.1), passage=passage) for position, passage in enumerate([big, huge, small], start=1))
        module = _FakeKnowledgeModule(result=KnowledgeSearchResult(hits=hits))
        tool_obj = _search_tool(module)

        message = _tool_message(await _invoke(tool_obj, query="预算"))

        assert message.status != "error"
        assert len(message.content.encode("utf-8")) <= 64 * 1024
        body = json.loads(message.content)
        assert [item["passage"][0] for item in body["items"]] == ["甲", "丙"]
        # Whole passages only: nothing was truncated into a prefix.
        assert [len(item["passage"]) for item in body["items"]] == [12_000, 2_000]
        assert body["delivered_count"] == 2
        assert body["omitted_count"] == 1
        assert body["context_limited"] is True
        # Citations describe exactly what was sent, in the ranking order.
        citations = message.additional_kwargs[KNOWLEDGE_CITATIONS_KEY]
        assert [item["segment_position"] for item in citations] == [1, 3]

    @pytest.mark.asyncio
    async def test_unfittable_first_passage_is_a_stable_error_without_citations(self) -> None:
        """Zero packed items is an error, never a fabricated empty result."""

        oversized = "满" * 30_000  # ~88 KiB alone
        module = _FakeKnowledgeModule(result=KnowledgeSearchResult(hits=(_hit(_citation(position=1), passage=oversized),)))
        tool_obj = _search_tool(module)

        message = _tool_message(await _invoke(tool_obj, query="超预算"))

        assert message.status == "error"
        assert message.content == "Error: KNOWLEDGE_PASSAGE_OVER_BUDGET: 命中正文超出工具消息预算，无法完整返回，请缩小查询范围"
        assert KNOWLEDGE_CITATIONS_KEY not in message.additional_kwargs

    @pytest.mark.asyncio
    async def test_knowledge_errors_become_error_toolmessages_without_citations(self) -> None:
        module = _FakeKnowledgeModule(error=KnowledgeError(KNOWLEDGE_SEARCH_FAILED, "检索失败了"))
        tool_obj = _search_tool(module)

        message = _tool_message(await _invoke(tool_obj, query="任何问题"))

        assert message.status == "error"
        assert message.content == f"Error: {KNOWLEDGE_SEARCH_FAILED}: 检索失败了"
        assert KNOWLEDGE_CITATIONS_KEY not in message.additional_kwargs


# ---------------------------------------------------------------------------
# knowledge_metadata_fields tool (read-only field discovery)
# ---------------------------------------------------------------------------


_BASE_ID = uuid.UUID("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")


def _filter_fields_fixture() -> list[KnowledgeBaseFilterFields]:
    return [
        KnowledgeBaseFilterFields(
            knowledge_base_id=_BASE_ID,
            knowledge_base_name="产品手册",
            description="面向客户的产品与安装文档",
            fields=(
                KnowledgeFilterFieldView(kind="builtin", name="document_name", field_type="string", operators=("eq", "contains"), writable=False),
                KnowledgeFilterFieldView(kind="custom", name="部门", field_type="string", operators=("eq", "contains"), writable=True),
            ),
        )
    ]


class _FakeFieldsModule:
    def __init__(self, *, error: KnowledgeError | None = None) -> None:
        self.calls: list[tuple[object, object]] = []
        self.authorities: list[object] = []
        self._error = error

    async def list_filter_fields(self, project_id, base_ids=None, *, authority):  # noqa: ANN001
        self.calls.append((project_id, base_ids))
        self.authorities.append(authority)
        if self._error is not None:
            raise self._error
        return _filter_fields_fixture()


def _fields_tool(module: _FakeFieldsModule):
    return create_knowledge_metadata_fields_tool(
        module,
        PROJECT_ID,
        _AUTHORITY,
    )


async def _invoke_fields(tool_obj, *, base_ids: list[str] | None = None, call_id: str = "call-f1"):  # noqa: ANN001
    args: dict[str, object] = {}
    if base_ids is not None:
        args["knowledge_base_ids"] = base_ids
    return await tool_obj.ainvoke(
        {
            "type": "tool_call",
            "name": KNOWLEDGE_METADATA_FIELDS_TOOL_NAME,
            "id": call_id,
            "args": args,
        }
    )


class TestKnowledgeMetadataFieldsTool:
    @pytest.mark.asyncio
    async def test_returns_definitions_only_with_closure_bound_authority(self) -> None:
        module = _FakeFieldsModule()
        tool_obj = _fields_tool(module)

        message = _tool_message(await _invoke_fields(tool_obj))

        # Identity comes from the closure; the model only chooses base ids.
        assert module.calls == [(PROJECT_ID, None)]
        assert module.authorities == [_AUTHORITY]
        assert set(tool_obj.tool_call_schema.model_fields) == {"knowledge_base_ids"}
        body = json.loads(message.content)
        assert body == {
            "bases": [
                {
                    "knowledge_base_id": str(_BASE_ID),
                    # Name and description let the model pick knowledge_base_ids
                    # for a scoped knowledge_search; still definitions only.
                    "name": "产品手册",
                    "description": "面向客户的产品与安装文档",
                    "fields": [
                        {"kind": "builtin", "name": "document_name", "field_type": "string", "operators": ["eq", "contains"], "writable": False},
                        {"kind": "custom", "name": "部门", "field_type": "string", "operators": ["eq", "contains"], "writable": True},
                    ],
                }
            ],
            "base_count": 1,
        }
        # Definitions only: no citations payload, no document values.
        assert KNOWLEDGE_CITATIONS_KEY not in message.additional_kwargs
        assert message.status != "error"

    @pytest.mark.asyncio
    async def test_invalid_base_id_strings_error_without_reaching_the_module(self) -> None:
        module = _FakeFieldsModule()
        tool_obj = _fields_tool(module)

        message = _tool_message(await _invoke_fields(tool_obj, base_ids=["不是UUID"]))

        assert message.status == "error"
        assert KNOWLEDGE_INVALID_REQUEST in message.content
        assert module.calls == []

    @pytest.mark.asyncio
    async def test_missing_capability_is_a_stable_error_toolmessage(self) -> None:
        from actweave_knowledge import KNOWLEDGE_FORBIDDEN

        module = _FakeFieldsModule(error=KnowledgeError(KNOWLEDGE_FORBIDDEN, "缺少 Knowledge 读取能力"))
        tool_obj = _fields_tool(module)

        message = _tool_message(await _invoke_fields(tool_obj))

        assert message.status == "error"
        assert KNOWLEDGE_FORBIDDEN in message.content


# ---------------------------------------------------------------------------
# Lead-agent factory wrapper (real graph assembly via canonical spies)
# ---------------------------------------------------------------------------


def _app_config() -> AppConfig:
    return AppConfig(
        models=[
            ModelConfig(
                name=MODEL_NAME,
                display_name="Knowledge tool test",
                description="",
                use="langchain_openai:ChatOpenAI",
                model="test-model",
                max_input_tokens=64_000,
                api_key=SecretStr("unit-test-key"),
                supports_thinking=False,
                supports_reasoning_effort=False,
            )
        ],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        summarization={"enabled": False},
        tool_search={"enabled": False},
        guardrails={"enabled": False},
    )


def _private_runtime(tmp_path: Path) -> object:
    return SimpleNamespace(
        model_ref=MODEL_NAME,
        model_settings=None,
        tool_groups=("web", "file:read"),
        skills=(),
        safe_manifest=SimpleNamespace(skills=()),
        mcp_tools=(),
        skill_root=tmp_path,
        prompt_bundle=None,
        soul="chat prompt",
        agent_catalog=None,
        capability_notice="",
        provider_request_closure_identity="closure-test",
        provider_request_mcp_closure_present=False,
    )


def _install_factory_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: dict[str, object],
) -> None:
    """Stub the graph-assembly edges of ``_make_lead_agent``.

    Same seam as tests/test_lead_agent_trusted_extension.py: tool selection,
    middleware manifest, prompt template and ``create_agent`` are spied so the
    test asserts the real extension plumbing without a model or checkpointer.
    """

    def available_tools(**kwargs):  # noqa: ANN003
        captured["available_tool_kwargs"] = kwargs
        return []

    def create_agent(**kwargs):  # noqa: ANN003
        captured["agent_kwargs"] = kwargs
        return "canonical-graph"

    def apply_prompt_template(**kwargs):  # noqa: ANN003
        captured.setdefault("prompt_calls", []).append(kwargs)
        return "canonical-prompt"

    monkeypatch.setattr(lead_agent_module, "frozen_checkpoint_channel_mode", lambda: None)
    monkeypatch.setattr(lead_agent_module, "freeze_checkpoint_channel_mode", lambda value: value)
    monkeypatch.setattr(lead_agent_module, "freeze_checkpoint_snapshot_frequency", lambda value: value)
    monkeypatch.setattr(lead_agent_module, "inject_checkpoint_mode", lambda *_args: None)
    monkeypatch.setattr(lead_agent_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(lead_agent_module, "build_middlewares", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(lead_agent_module, "normalize_middleware_state_schemas", lambda value, *_args: value)
    monkeypatch.setattr(lead_agent_module, "apply_prompt_template", apply_prompt_template)
    monkeypatch.setattr(lead_agent_module, "get_thread_state_schema", lambda *_args: dict)
    monkeypatch.setattr(
        lead_agent_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: object(),
    )
    monkeypatch.setattr(lead_agent_module, "create_agent", create_agent)
    monkeypatch.setattr("deerflow.tools.get_available_tools", available_tools)


class TestKnowledgeLeadAgentFactory:
    def test_factory_refuses_a_caller_supplied_trusted_extension(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        captured: dict[str, object] = {}
        _install_factory_spies(monkeypatch, captured=captured)
        factory = create_knowledge_lead_agent_factory(
            module=_FakeKnowledgeModule(),
            project_id=PROJECT_ID,
            owner_user_id=OWNER_USER_ID,
            authority=_AUTHORITY,
        )

        with pytest.raises(ValueError, match="trusted extension"):
            factory.private_runtime_factory(
                config={"configurable": {"thinking_enabled": False}},
                private_runtime=_private_runtime(tmp_path),
                app_config=_app_config(),
                trusted_extension=TrustedLeadAgentExtension(),
            )

    def test_base_factory_with_a_custom_private_runtime_path_is_refused(self) -> None:
        def base_factory(config):  # noqa: ANN001
            return object()

        base_factory.private_runtime_factory = lambda **_kwargs: None

        with pytest.raises(ValueError, match="private_runtime_factory"):
            create_knowledge_lead_agent_factory(
                module=_FakeKnowledgeModule(),
                project_id=PROJECT_ID,
                owner_user_id=OWNER_USER_ID,
                authority=_AUTHORITY,
                base_factory=base_factory,
            )


# ---------------------------------------------------------------------------
# Executor factory resolution (the feature flag's only production wiring)
# ---------------------------------------------------------------------------


def _executor_with(knowledge_module: object | None):
    from app.reliability.run_execution.executor import RunAgentPrivateExecutor

    executor = object.__new__(RunAgentPrivateExecutor)
    executor._factory = object()
    executor._knowledge_module = knowledge_module
    return executor


def _execution(runtime_kind: str = "chat") -> SimpleNamespace:
    context = ProjectContext(
        user_id=OWNER_USER_ID,
        project_id=PROJECT_ID,
        membership_id=uuid.UUID("eeeeeeee-5555-4555-8555-eeeeeeeeeeee"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id="req-knowledge-agent-tool",
    )
    return SimpleNamespace(
        runtime_kind=runtime_kind,
        context=PrivateWorkContext.from_project(context),
    )


class TestExecutorFactoryResolution:
    @pytest.mark.asyncio
    async def test_chat_run_with_module_gets_knowledge_search_bound_to_the_run_project(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        module = _FakeKnowledgeModule()
        executor = _executor_with(module)
        base = object()

        resolved = executor._resolve_agent_factory(_execution("chat"), object(), base)

        assert resolved is not base
        captured: dict[str, object] = {}
        _install_factory_spies(monkeypatch, captured=captured)
        resolved.private_runtime_factory(
            config={"configurable": {"thinking_enabled": False}},
            private_runtime=_private_runtime(tmp_path),
            app_config=_app_config(),
        )
        tools = {tool.name: tool for tool in captured["agent_kwargs"]["tools"]}
        assert KNOWLEDGE_SEARCH_TOOL_NAME in tools

        await _invoke(tools[KNOWLEDGE_SEARCH_TOOL_NAME], query="接线验证")
        assert module.requests[0].project_id == PROJECT_ID
        assert module.requests[0].owner_user_id == OWNER_USER_ID

    def test_skill_builder_run_never_receives_the_knowledge_tool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.reliability.run_execution.executor as executor_module

        sentinel = object()
        monkeypatch.setattr(
            executor_module,
            "SkillBuilderAgentFactory",
            lambda *, catalog, draft_sink: sentinel,
        )
        monkeypatch.setattr(
            executor_module,
            "WorkerSkillBuilderAuthoringCatalog",
            lambda _factory, _context: object(),
        )

        fake_sink = object()
        sink_constructions: list[tuple[object, object, object]] = []

        def _draft_sink(factory, context, claim):  # noqa: ANN001
            sink_constructions.append((factory, context, claim))
            return fake_sink

        monkeypatch.setattr(executor_module, "SkillDesignDraftSink", _draft_sink)
        # Knowledge module present: the skill_builder branch must still win.
        executor = _executor_with(_FakeKnowledgeModule())
        execution = _execution("skill_builder")
        claim = object()

        resolved = executor._resolve_agent_factory(execution, claim, object())

        assert resolved is sentinel
        assert sink_constructions == [(executor._factory, execution.context, claim)]

    def test_chat_run_without_module_returns_the_base_factory_unchanged(self) -> None:
        executor = _executor_with(None)
        base = object()

        assert executor._resolve_agent_factory(_execution("chat"), object(), base) is base


# ---------------------------------------------------------------------------
# Journal Command persistence (the refresh-recovery chain's first link)
# ---------------------------------------------------------------------------


class TestJournalCommandPersistence:
    @staticmethod
    def _journal(persisted: list[dict]):
        from deerflow.runtime.journal import RunJournal

        class _EventStore:
            async def put_batch(self, events, **_kwargs):  # noqa: ANN001, ANN003
                persisted.extend(events)

        return RunJournal("run-journal-1", "thread-journal-1", _EventStore())

    @pytest.mark.asyncio
    async def test_command_toolmessage_persists_as_message_with_citations_intact(self) -> None:
        persisted: list[dict] = []
        journal = self._journal(persisted)
        citations = [
            {
                "knowledge_base_id": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
                "segment_id": "dddddddd-4444-4444-8444-dddddddddddd",
                "snippet": "第 1 段:量子比特……",
                "score": 0.93,
            }
        ]

        journal.on_tool_end(
            Command(
                update={
                    "messages": [
                        ToolMessage(
                            '{"items": []}',
                            tool_call_id="call-journal-1",
                            additional_kwargs={KNOWLEDGE_CITATIONS_KEY: citations},
                        )
                    ]
                }
            ),
            run_id=uuid.uuid4(),
            tags=["lead_agent"],
        )
        await journal.flush()

        [event] = persisted
        assert event["event_type"] == "llm.tool.result"
        assert event["category"] == "message"
        content = event["content"]
        assert content["tool_call_id"] == "call-journal-1"
        assert content["additional_kwargs"][KNOWLEDGE_CITATIONS_KEY] == citations

    @pytest.mark.asyncio
    async def test_command_error_toolmessage_keeps_status_and_carries_no_citations(self) -> None:
        persisted: list[dict] = []
        journal = self._journal(persisted)

        journal.on_tool_end(
            Command(
                update={
                    "messages": [
                        ToolMessage(
                            "Error: KNOWLEDGE_SEARCH_FAILED: 检索失败了",
                            tool_call_id="call-journal-2",
                            status="error",
                        )
                    ]
                }
            ),
            run_id=uuid.uuid4(),
            tags=["lead_agent"],
        )
        await journal.flush()

        [event] = persisted
        assert event["category"] == "message"
        content = event["content"]
        assert content["status"] == "error"
        assert KNOWLEDGE_CITATIONS_KEY not in content["additional_kwargs"]
