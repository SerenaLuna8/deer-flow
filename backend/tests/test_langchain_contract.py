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

import importlib.metadata
from collections.abc import Iterable

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.tools import tool
from langgraph.channels.binop import BinaryOperatorAggregate
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import InvalidUpdateError
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.types import Overwrite
from packaging.requirements import Requirement
from packaging.version import Version

import deerflow.checkpoint_patches as checkpoint_patches


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


# ---------------------------------------------------------------------------
# Group 3 — create_agent accepts the exact production parameter shape
# ---------------------------------------------------------------------------


def test_create_agent_accepts_the_production_parameter_shape() -> None:
    """All three ActWeave call sites pass exactly these keyword arguments."""

    from deerflow.agents.thread_state import get_thread_state_schema

    checkpointer = InMemorySaver()
    agent = create_agent(
        model=_fake_model([AIMessage(content="done")]),
        tools=[echo],
        middleware=[_recorder("only", [])],
        system_prompt="You are a contract test.",
        state_schema=get_thread_state_schema("full", 10),
        checkpointer=checkpointer,
        name="contract-agent",
    )
    result = agent.invoke(
        {"messages": [HumanMessage(content="hi")]},
        {"configurable": {"thread_id": "contract-thread"}},
    )

    assert result["messages"][-1].content == "done"
    saved = checkpointer.get({"configurable": {"thread_id": "contract-thread"}})
    assert saved is not None


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
