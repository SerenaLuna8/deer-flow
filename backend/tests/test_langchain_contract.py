"""U5 — upstream behavior contracts ActWeave's middleware chain depends on.

About thirty middlewares assume specific LangChain/LangGraph *behavior*, not
just signatures: ``after_model`` dispatches in reverse registration order,
``wrap_model_call``/``wrap_tool_call`` compose first-registered-outermost,
``create_agent`` accepts the exact production parameter shape, and
``checkpoint_patches.py`` overrides two precise upstream internals. None of
these are covered by upstream semver promises, so this module is the canary:
run it first after any ``langchain*``/``langgraph*`` bump (see
``docs/langgraph-upgrade-playbook.zh-CN.md``).
"""

from __future__ import annotations

import ast
import importlib.metadata
import inspect
from collections.abc import Iterable
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.channels.binop import BinaryOperatorAggregate
from langgraph.checkpoint.base import BaseCheckpointSaver, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import InvalidUpdateError
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.types import Overwrite
from packaging.requirements import Requirement
from packaging.version import Version

import deerflow.checkpoint_patches as checkpoint_patches

# The patch module captures these methods before its process-wide assignment.
# Reading that canonical capture keeps the before/after probe independent of
# pytest collection order: another test may import thread_state (and therefore
# apply the patch) before this module is collected.
_UPSTREAM_INMEMORY_GET_DELTA_HISTORY = checkpoint_patches._unpatched_inmemory_get_delta_channel_history
_UPSTREAM_INMEMORY_AGET_DELTA_HISTORY = checkpoint_patches._unpatched_inmemory_aget_delta_channel_history


class _ToolBindingFakeModel(GenericFakeChatModel):
    """GenericFakeChatModel that tolerates create_agent's bind_tools call."""

    def bind_tools(self, tools, **kwargs):
        return self


def _fake_model(responses: Iterable[AIMessage]) -> GenericFakeChatModel:
    return _ToolBindingFakeModel(messages=iter(responses))


@tool
def echo(text: str) -> str:
    """Echo the given text back."""

    return text


def _recorder(label: str, journal: list[str]) -> AgentMiddleware:
    """Build a journal-recording middleware with a unique class identity.

    ``AgentMiddleware.name`` derives from the class, so each recorder gets
    its own dynamic subclass rather than sharing one class between labels.
    """

    class _Recorder(AgentMiddleware):
        def before_model(self, state, runtime):
            journal.append(f"{label}:before_model")
            return None

        def after_model(self, state, runtime):
            journal.append(f"{label}:after_model")
            return None

        def wrap_model_call(self, request, handler):
            journal.append(f"{label}:model_enter")
            response = handler(request)
            journal.append(f"{label}:model_exit")
            return response

        def wrap_tool_call(self, request, handler):
            journal.append(f"{label}:tool_enter")
            result = handler(request)
            journal.append(f"{label}:tool_exit")
            return result

    _Recorder.__name__ = f"Recorder_{label.title()}"
    _Recorder.__qualname__ = _Recorder.__name__
    return _Recorder()


# ---------------------------------------------------------------------------
# Group 1 — after_model dispatches in REVERSE registration order
# ---------------------------------------------------------------------------


def test_after_model_dispatches_in_reverse_registration_order() -> None:
    """SafetyFinishReason/LoopDetection placement is built on this behavior."""

    journal: list[str] = []
    agent = create_agent(
        model=_fake_model([AIMessage(content="done")]),
        middleware=[_recorder("first", journal), _recorder("second", journal)],
    )
    agent.invoke({"messages": [HumanMessage(content="hi")]})

    before = [entry for entry in journal if entry.endswith(":before_model")]
    after = [entry for entry in journal if entry.endswith(":after_model")]
    assert before == ["first:before_model", "second:before_model"]
    assert after == ["second:after_model", "first:after_model"]


# ---------------------------------------------------------------------------
# Group 2 — wrap_model_call / wrap_tool_call compose first-registered-outermost
# ---------------------------------------------------------------------------


def test_wrap_model_call_composes_first_registered_outermost() -> None:
    """InputSanitization must stay the outermost model-call wrapper."""

    journal: list[str] = []
    agent = create_agent(
        model=_fake_model([AIMessage(content="done")]),
        middleware=[_recorder("outer", journal), _recorder("inner", journal)],
    )
    agent.invoke({"messages": [HumanMessage(content="hi")]})

    model_events = [entry for entry in journal if ":model_" in entry]
    assert model_events == [
        "outer:model_enter",
        "inner:model_enter",
        "inner:model_exit",
        "outer:model_exit",
    ]


def test_wrap_tool_call_composes_first_registered_outermost() -> None:
    """ToolProgress (outer) must enclose ToolErrorHandling's result stamping."""

    journal: list[str] = []
    agent = create_agent(
        model=_fake_model(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "echo", "args": {"text": "ping"}, "id": "call-1"}],
                ),
                AIMessage(content="done"),
            ]
        ),
        tools=[echo],
        middleware=[_recorder("outer", journal), _recorder("inner", journal)],
    )
    result = agent.invoke({"messages": [HumanMessage(content="hi")]})

    tool_events = [entry for entry in journal if ":tool_" in entry]
    assert tool_events == [
        "outer:tool_enter",
        "inner:tool_enter",
        "inner:tool_exit",
        "outer:tool_exit",
    ]
    assert result["messages"][-1].content == "done"


def test_wrap_model_call_can_short_circuit_every_inner_layer() -> None:
    """An outer admission gate may return a response without invoking the model."""

    journal: list[str] = []

    class _ShortCircuit(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            del request, handler
            journal.append("short:model")
            return ModelResponse(result=[AIMessage(content="short-circuited")])

    agent = create_agent(
        model=_fake_model([AIMessage(content="must-not-run")]),
        middleware=[_ShortCircuit(), _recorder("inner", journal)],
    )
    result = agent.invoke({"messages": [HumanMessage(content="hi")]})

    assert result["messages"][-1].content == "short-circuited"
    assert journal == ["inner:before_model", "short:model", "inner:after_model"]


def test_wrap_tool_call_can_short_circuit_every_inner_layer() -> None:
    """An outer tool gate may deny a call without reaching inner wrappers/tool code."""

    journal: list[str] = []

    class _ShortCircuit(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            del handler
            journal.append("short:tool")
            return ToolMessage(
                content="blocked",
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
            )

    agent = create_agent(
        model=_fake_model(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "echo",
                            "args": {"text": "must-not-run"},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        ),
        tools=[echo],
        middleware=[_ShortCircuit(), _recorder("inner", journal)],
    )
    result = agent.invoke({"messages": [HumanMessage(content="hi")]})

    assert result["messages"][-1].content == "done"
    assert "short:tool" in journal
    assert not any(entry.startswith("inner:tool_") for entry in journal)


# ---------------------------------------------------------------------------
# Group 3 — create_agent accepts every exact production parameter shape
# ---------------------------------------------------------------------------


def _direct_create_agent_keywords(relative_path: str) -> set[str]:
    source_path = Path(__file__).resolve().parents[1] / "packages" / "harness" / relative_path
    tree = ast.parse(source_path.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "create_agent"]
    assert len(calls) == 1, relative_path
    assert all(keyword.arg is not None for keyword in calls[0].keywords), relative_path
    return {keyword.arg for keyword in calls[0].keywords if keyword.arg is not None}


def _embedded_create_agent_keywords() -> tuple[set[str], set[str]]:
    source_path = Path(__file__).resolve().parents[1] / "packages" / "harness" / "deerflow/client.py"
    tree = ast.parse(source_path.read_text())
    ensure_agent = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_ensure_agent")
    kwargs_literal = next(node.value for node in ast.walk(ensure_agent) if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "kwargs" and isinstance(node.value, ast.Dict))
    required = {key.value for key in kwargs_literal.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    optional = {
        target.slice.value
        for node in ast.walk(ensure_agent)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == "kwargs" and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str)
    }
    calls = [node for node in ast.walk(ensure_agent) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "create_agent"]
    assert len(calls) == 1
    assert len(calls[0].keywords) == 1 and calls[0].keywords[0].arg is None
    return required, optional


def test_production_create_agent_callsite_shapes_are_exhaustive() -> None:
    """Map the contract cases to the four concrete production call sites."""

    base = {"model", "tools", "middleware", "system_prompt", "state_schema"}
    assert _direct_create_agent_keywords("deerflow/agents/lead_agent/agent.py") == base
    assert _direct_create_agent_keywords("deerflow/agents/factory.py") == {
        *base,
        "checkpointer",
        "name",
    }
    assert _direct_create_agent_keywords("deerflow/subagents/executor.py") == {
        *base,
        "checkpointer",
    }
    embedded_required, embedded_optional = _embedded_create_agent_keywords()
    assert embedded_required == base
    assert embedded_optional == {"checkpointer"}


@pytest.mark.parametrize(
    ("callsite", "expected_keys", "checkpointer", "name"),
    [
        (
            "lead_agent/agent.py",
            {"model", "tools", "middleware", "system_prompt", "state_schema"},
            "absent",
            "absent",
        ),
        (
            "agents/factory.py",
            {
                "model",
                "tools",
                "middleware",
                "system_prompt",
                "state_schema",
                "checkpointer",
                "name",
            },
            "memory",
            "contract-sdk-agent",
        ),
        (
            "subagents/executor.py",
            {
                "model",
                "tools",
                "middleware",
                "system_prompt",
                "state_schema",
                "checkpointer",
            },
            False,
            "absent",
        ),
        (
            "client.py-without-checkpointer",
            {"model", "tools", "middleware", "system_prompt", "state_schema"},
            "absent",
            "absent",
        ),
        (
            "client.py-with-checkpointer",
            {
                "model",
                "tools",
                "middleware",
                "system_prompt",
                "state_schema",
                "checkpointer",
            },
            "memory",
            "absent",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_create_agent_accepts_each_production_parameter_shape(
    callsite: str,
    expected_keys: set[str],
    checkpointer: object,
    name: object,
) -> None:
    """Replay all four direct call sites, including the embedded optional key."""

    from deerflow.agents.thread_state import get_thread_state_schema

    kwargs = {
        "model": _fake_model([AIMessage(content=f"done:{callsite}")]),
        "tools": [echo],
        "middleware": [_recorder("only", [])],
        "system_prompt": "You are a contract test.",
        "state_schema": get_thread_state_schema("full", 10),
    }
    saver = None
    if checkpointer == "memory":
        saver = InMemorySaver()
        kwargs["checkpointer"] = saver
    elif checkpointer != "absent":
        kwargs["checkpointer"] = checkpointer
    if name != "absent":
        kwargs["name"] = name

    assert set(kwargs) == expected_keys
    agent = create_agent(**kwargs)
    config = {"configurable": {"thread_id": f"contract-{callsite}"}}
    result = agent.invoke({"messages": [HumanMessage(content="hi")]}, config)

    assert result["messages"][-1].content == f"done:{callsite}"
    if saver is not None:
        assert saver.get(config) is not None


# ---------------------------------------------------------------------------
# Group 4 — checkpoint_patches.py upstream internals
# ---------------------------------------------------------------------------


def test_inmemory_delta_history_patch_applied_or_stood_down() -> None:
    """Either our delegate replaced the buggy override, or upstream removed it.

    Any third state (override present but unpatched) means the patch guard
    regressed and full->delta migrated threads silently drop writes again.
    """

    patched = getattr(InMemorySaver, "_deerflow_delta_history_patched", False)
    if patched:
        assert InMemorySaver.get_delta_channel_history is checkpoint_patches._get_delta_channel_history_via_base
        assert InMemorySaver.aget_delta_channel_history is checkpoint_patches._aget_delta_channel_history_via_base
    else:
        assert not checkpoint_patches._upstream_override_present(), "InMemorySaver still ships its own delta-history override but the patch did not engage"
    # The delegate target must keep existing upstream.
    assert callable(getattr(BaseCheckpointSaver, "get_delta_channel_history", None))
    assert callable(getattr(BaseCheckpointSaver, "aget_delta_channel_history", None))


@pytest.mark.asyncio
async def test_inmemory_delta_history_patch_contract_before_and_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the pinned upstream loss, patch the real class, and recover it."""

    def parameter_shape(callable_):
        return tuple((parameter.name, parameter.kind) for parameter in inspect.signature(callable_).parameters.values())

    expected_shape = parameter_shape(BaseCheckpointSaver.get_delta_channel_history)
    assert parameter_shape(_UPSTREAM_INMEMORY_GET_DELTA_HISTORY) == expected_shape
    assert parameter_shape(_UPSTREAM_INMEMORY_AGET_DELTA_HISTORY) == expected_shape
    assert parameter_shape(checkpoint_patches._get_delta_channel_history_via_base) == expected_shape
    assert parameter_shape(checkpoint_patches._aget_delta_channel_history_via_base) == expected_shape

    monkeypatch.setattr(
        InMemorySaver,
        "get_delta_channel_history",
        _UPSTREAM_INMEMORY_GET_DELTA_HISTORY,
    )
    monkeypatch.setattr(
        InMemorySaver,
        "aget_delta_channel_history",
        _UPSTREAM_INMEMORY_AGET_DELTA_HISTORY,
    )
    monkeypatch.delattr(InMemorySaver, checkpoint_patches._PATCH_FLAG, raising=False)

    saver = InMemorySaver()
    root_config = {"configurable": {"thread_id": "delta-history-contract", "checkpoint_ns": ""}}
    full_checkpoint = empty_checkpoint()
    full_checkpoint["channel_values"] = {"messages": ["old"]}
    full_checkpoint["channel_versions"] = {"messages": "v1"}
    full_config = saver.put(
        root_config,
        full_checkpoint,
        {},
        {"messages": "v1"},
    )
    saver.put_writes(full_config, [("messages", "new")], "task")
    carried_checkpoint = empty_checkpoint()
    carried_checkpoint["channel_values"] = {"messages": ["old"]}
    carried_checkpoint["channel_versions"] = {"messages": "v1"}
    carried_config = saver.put(full_config, carried_checkpoint, {}, {})

    before_sync = saver.get_delta_channel_history(
        config=carried_config,
        channels=["messages"],
    )
    before_async = await saver.aget_delta_channel_history(
        config=carried_config,
        channels=["messages"],
    )
    assert before_sync == before_async
    assert before_sync["messages"] == {"writes": [], "seed": ["old"]}

    assert checkpoint_patches._upstream_override_present() is True

    checkpoint_patches.ensure_inmemory_delta_history_patch()

    assert getattr(InMemorySaver, checkpoint_patches._PATCH_FLAG, False)
    assert InMemorySaver.get_delta_channel_history is checkpoint_patches._get_delta_channel_history_via_base
    assert InMemorySaver.aget_delta_channel_history is checkpoint_patches._aget_delta_channel_history_via_base

    after_sync = saver.get_delta_channel_history(
        config=carried_config,
        channels=["messages"],
    )
    after_async = await saver.aget_delta_channel_history(
        config=carried_config,
        channels=["messages"],
    )
    assert after_sync == after_async
    assert after_sync["messages"] == {
        "writes": [("task", "messages", "new")],
        "seed": ["old"],
    }


def test_binop_overwrite_first_write_lands_unwrapped() -> None:
    """Union-typed channels must never persist the Overwrite wrapper itself."""

    channel = BinaryOperatorAggregate(dict | None, lambda existing, new: new)
    channel.key = "contract-probe"
    channel.update([Overwrite({"probe": True})])
    assert channel.get() == {"probe": True}

    fresh = BinaryOperatorAggregate(dict | None, lambda existing, new: new)
    fresh.key = "contract-probe-two"
    with pytest.raises(InvalidUpdateError):
        fresh.update([Overwrite({"a": 1}), Overwrite({"b": 2})])


def test_binop_overwrite_patch_contract_before_and_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore the pinned upstream bug, apply the patch, then rerun its probe."""

    monkeypatch.setattr(
        BinaryOperatorAggregate,
        "update",
        checkpoint_patches._unpatched_binop_update,
    )
    monkeypatch.delattr(
        BinaryOperatorAggregate,
        checkpoint_patches._BINOP_PATCH_FLAG,
        raising=False,
    )

    assert checkpoint_patches._binop_first_write_stores_overwrite_wrapper() is True

    checkpoint_patches.ensure_binop_overwrite_first_write_patch()

    assert getattr(
        BinaryOperatorAggregate,
        checkpoint_patches._BINOP_PATCH_FLAG,
        False,
    )
    assert BinaryOperatorAggregate.update is checkpoint_patches._binop_update_unwrapping_empty_channel
    assert checkpoint_patches._binop_first_write_stores_overwrite_wrapper() is False


def test_remove_all_messages_sentinel_still_resets_history() -> None:
    """Summarization compaction rebuilds history via this exact sentinel."""

    kept = add_messages(
        [HumanMessage(content="old", id="m1"), AIMessage(content="older", id="m2")],
        [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content="new", id="m3")],
    )
    assert [message.content for message in kept] == ["new"]


# ---------------------------------------------------------------------------
# Version fence — declared bounds and installed versions must agree
# ---------------------------------------------------------------------------

_BOUNDED_PACKAGES = ("langchain", "langchain-core", "langgraph")


def _declared_requirements() -> dict[str, Requirement]:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "packages" / "harness" / "pyproject.toml"
    raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requirements = {}
    for item in raw["project"]["dependencies"]:
        requirement = Requirement(item)
        requirements[requirement.name] = requirement
    return requirements


def test_upstream_packages_declare_upper_bounds() -> None:
    """A bare ``>=`` invites an accidental major jump via ``uv lock --upgrade``."""

    declared = _declared_requirements()
    for package in _BOUNDED_PACKAGES:
        assert package in declared, f"{package} must be a direct declared dependency"
        specifiers = str(declared[package].specifier)
        assert "<" in specifiers, f"{package} needs an upper bound, has only {specifiers!r}"


def test_installed_upstream_versions_satisfy_the_declared_bounds() -> None:
    declared = _declared_requirements()
    for package in _BOUNDED_PACKAGES:
        installed = Version(importlib.metadata.version(package))
        assert declared[package].specifier.contains(str(installed), prereleases=True), f"{package} {installed} violates declared bound {declared[package].specifier}"
