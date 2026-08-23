"""Interface acceptance for Harness tool-call control."""

import random
from collections.abc import Iterable
from typing import get_args

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.read_before_write_middleware import (
    ReadBeforeWriteMiddleware,
)
from deerflow.agents.middlewares.tool_call_control import (
    TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY,
    TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY,
    TOOL_CALL_CONTROL_RECEIPT_KEY,
    TOOL_CALL_CONTROL_STATE_KEY,
    FixedToolCallControlScope,
    PerInvocationToolCallControlScope,
    RepeatedCallPolicy,
    ResolvedToolCallBudgetPolicy,
    ResolvedToolCallControlPolicy,
    ToolCallControlBinding,
    ToolCallControlReasonCode,
    ToolCallLimit,
    build_tool_call_control,
    default_graph_tool_call_control_profile,
)
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.runs.execution_contracts import RunSemanticStopRecorder
from deerflow.vision.dispatch import MAX_VISION_CALLS_PER_RUN


class _ToolBindingFakeModel(GenericFakeChatModel):
    seen_messages: list[list[BaseMessage]] = []
    bound_tool_names: list[list[str]] = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names.append([tool.name if hasattr(tool, "name") else str(tool.get("name", "")) for tool in tools])
        del kwargs
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages.append(list(messages))
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


def _model(responses: Iterable[AIMessage]) -> _ToolBindingFakeModel:
    model = _ToolBindingFakeModel(messages=iter(responses))
    model.seen_messages = []
    model.bound_tool_names = []
    return model


def _policy(*, web_warn: int = 6, web_hard: int = 10) -> ResolvedToolCallControlPolicy:
    return ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=100,
            hard_limit=101,
            window_size=200,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=100, hard_limit=101),
            tools={
                "web_search": ToolCallLimit(
                    warn_threshold=web_warn,
                    hard_limit=web_hard,
                ),
                "web_fetch": ToolCallLimit(
                    warn_threshold=web_warn,
                    hard_limit=web_hard,
                ),
            },
        ),
    )


def test_batch_budget_admits_exact_prefix_and_keeps_other_tools() -> None:
    web_queries: list[str] = []
    fetched_urls: list[str] = []
    written_paths: list[str] = []
    presented_paths: list[str] = []

    @tool
    def web_search(query: str) -> str:
        """Search one query."""

        web_queries.append(query)
        return f"result:{query}"

    @tool
    def web_fetch(url: str) -> str:
        """Fetch one URL."""

        fetched_urls.append(url)
        return f"content:{url}"

    @tool
    def write_file(path: str) -> str:
        """Write one result file."""

        written_paths.append(path)
        return "written"

    @tool
    def present_files(paths: list[str]) -> str:
        """Present completed files."""

        presented_paths.extend(paths)
        return "presented"

    responses = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {"query": f"prior-{index}"},
                    "id": f"prior-search-{index}",
                },
                {
                    "name": "web_fetch",
                    "args": {"url": f"https://example.test/prior-{index}"},
                    "id": f"prior-fetch-{index}",
                },
            ],
        )
        for index in range(9)
    ]
    responses.extend(
        [
            AIMessage(
                id="boundary-batch",
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "admitted"},
                        "id": "boundary-1",
                    },
                    {
                        "name": "web_search",
                        "args": {"query": "rejected-1"},
                        "id": "boundary-2",
                    },
                    {
                        "name": "web_search",
                        "args": {"query": "rejected-2"},
                        "id": "boundary-3",
                    },
                    {
                        "name": "web_fetch",
                        "args": {"url": "https://example.test/admitted"},
                        "id": "boundary-fetch-1",
                    },
                    {
                        "name": "web_fetch",
                        "args": {"url": "https://example.test/rejected-1"},
                        "id": "boundary-fetch-2",
                    },
                    {
                        "name": "web_fetch",
                        "args": {"url": "https://example.test/rejected-2"},
                        "id": "boundary-fetch-3",
                    },
                    {
                        "name": "write_file",
                        "args": {"path": "outputs/report.md"},
                        "id": "boundary-4",
                    },
                    {
                        "name": "present_files",
                        "args": {"paths": ["outputs/report.md"]},
                        "id": "boundary-5",
                    },
                ],
            ),
            AIMessage(id="final", content="research complete"),
        ]
    )
    model = _model(responses)
    agent = create_agent(
        model=model,
        tools=[web_search, web_fetch, write_file, present_files],
        middleware=[
            build_tool_call_control(
                _policy(),
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-1"),
                    workload_profile="research",
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="research and write the report")]},
        context={"run_id": "run-1"},
    )

    assert web_queries == [*[f"prior-{index}" for index in range(9)], "admitted"]
    assert fetched_urls == [
        *[f"https://example.test/prior-{index}" for index in range(9)],
        "https://example.test/admitted",
    ]
    assert written_paths == ["outputs/report.md"]
    assert presented_paths == ["outputs/report.md"]
    assert result["messages"][-1].content == "research complete"
    assert model.bound_tool_names[-1] == ["write_file", "present_files"]


def test_random_batches_never_admit_more_than_the_hard_limit() -> None:
    random_source = random.Random(20260823)

    for case_index in range(100):
        hard_limit = random_source.randint(1, 12)
        middleware = build_tool_call_control(
            ResolvedToolCallControlPolicy(
                repeated_calls=RepeatedCallPolicy(
                    enabled=False,
                    warn_threshold=1,
                    hard_limit=2,
                    window_size=2,
                ),
                tool_budget=ResolvedToolCallBudgetPolicy(
                    default=ToolCallLimit(
                        warn_threshold=100,
                        hard_limit=101,
                    ),
                    tools={
                        "web_search": ToolCallLimit(
                            warn_threshold=1,
                            hard_limit=hard_limit,
                        )
                    },
                ),
            ),
            ToolCallControlBinding(
                role="lead",
                scope=FixedToolCallControlScope(f"run-random-{case_index}"),
            ),
        )
        control_state: dict | None = None
        admitted_total = 0
        for batch_index in range(random_source.randint(1, 20)):
            batch_size = random_source.randint(1, 12)
            proposal = AIMessage(
                id=f"proposal-{case_index}-{batch_index}",
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {
                            "query": f"query-{case_index}-{batch_index}-{offset}",
                        },
                        "id": f"call-{case_index}-{batch_index}-{offset}",
                    }
                    for offset in range(batch_size)
                ],
            )
            state = {"messages": [proposal]}
            if control_state is not None:
                state[TOOL_CALL_CONTROL_STATE_KEY] = control_state

            update = middleware.after_model(state, Runtime(context={}))

            assert update is not None
            rewritten = update["messages"][0]
            admitted_total += len(rewritten.tool_calls)
            control_state = update[TOOL_CALL_CONTROL_STATE_KEY]
            assert admitted_total == control_state["admitted_counts"].get(
                "web_search",
                0,
            )
            assert admitted_total <= hard_limit


@pytest.mark.asyncio
async def test_failed_tool_execution_still_consumes_its_admitted_budget() -> None:
    attempts = 0

    @tool
    async def unstable_lookup(value: str) -> str:
        """Fail while looking up one value."""

        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"lookup failed: {value}")

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=2,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=1),
            tools={},
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="failing-proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "unstable_lookup",
                            "args": {"value": "first"},
                            "id": "failing-call",
                        }
                    ],
                ),
                AIMessage(
                    id="over-budget-proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "unstable_lookup",
                            "args": {"value": "second"},
                            "id": "over-budget-call",
                        }
                    ],
                ),
                AIMessage(id="final", content="continued with the failure fact"),
            ]
        ),
        tools=[unstable_lookup],
        middleware=[
            ToolErrorHandlingMiddleware(),
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-failed-tool-budget"),
                ),
            ),
        ],
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="try the lookup and continue")]},
        context={"run_id": "run-failed-tool-budget"},
    )

    assert attempts == 1
    rejected = next(message for message in result["messages"] if isinstance(message, AIMessage) and message.id == "over-budget-proposal")
    assert rejected.tool_calls == []
    assert result["messages"][-1].content == "continued with the failure fact"


def test_budget_warning_is_advisory_after_tool_message_pairing() -> None:
    calls: list[str] = []
    observations: list[object] = []

    @tool
    def web_search(query: str) -> str:
        """Search one query."""

        calls.append(query)
        return f"result:{query}"

    class _Observer:
        def observe(self, observation: object) -> None:
            observations.append(observation)

    responses = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {"query": f"prior-{index}"},
                    "id": f"call-{index}",
                }
            ],
        )
        for index in range(5)
    ]
    responses.extend(
        [
            AIMessage(
                id="warning-batch",
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": f"boundary-{index}"},
                        "id": f"boundary-{index}",
                    }
                    for index in range(3)
                ],
            ),
            AIMessage(id="final", content="continued after the advisory"),
        ]
    )
    model = _model(responses)
    agent = create_agent(
        model=model,
        tools=[web_search],
        middleware=[
            build_tool_call_control(
                _policy(),
                ToolCallControlBinding(
                    role="subagent",
                    scope=FixedToolCallControlScope("execution-1"),
                    workload_profile="research",
                    observer=_Observer(),
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="continue researching")]},
        context={"run_id": "run-1"},
    )

    assert calls[:5] == [f"prior-{index}" for index in range(5)]
    assert sorted(calls[5:]) == [f"boundary-{index}" for index in range(3)]
    assert result["messages"][-1].content == "continued after the advisory"
    warning_request = model.seen_messages[-1]
    assert isinstance(warning_request[-2], ToolMessage)
    assert isinstance(warning_request[-1], HumanMessage)
    warning = str(warning_request[-1].content)
    assert "8 of 10 web_search calls" in warning
    assert "Stop calling tools" not in warning
    assert "produce your final answer now" not in warning

    assert len(observations) == 1
    observation = observations[0]
    assert observation.reason_code == "tool_budget_warning"
    assert observation.count_before == 5
    assert observation.proposed == 3
    assert observation.admitted == 3
    assert observation.rejected == 0
    assert observation.count_after == 8


def test_duplicate_tool_call_ids_are_filtered_by_occurrence() -> None:
    calls: list[str] = []

    @tool
    def web_search(query: str) -> str:
        """Search one query."""

        calls.append(query)
        return f"result:{query}"

    responses = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {"query": f"prior-{index}"},
                    "id": f"prior-{index}",
                }
            ],
        )
        for index in range(9)
    ]
    raw_calls = [
        {
            "id": "duplicate-id",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": f'{{"query":"boundary-{index}"}}',
            },
        }
        for index in range(3)
    ]
    responses.extend(
        [
            AIMessage(
                id="duplicate-boundary",
                content="",
                additional_kwargs={"tool_calls": raw_calls},
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": f"boundary-{index}"},
                        "id": "duplicate-id",
                    }
                    for index in range(3)
                ],
            ),
            AIMessage(id="final", content="done"),
        ]
    )
    agent = create_agent(
        model=_model(responses),
        tools=[web_search],
        middleware=[
            build_tool_call_control(
                _policy(),
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-duplicates"),
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="research")]},
        context={"run_id": "run-duplicates"},
    )

    boundary = next(message for message in result["messages"] if isinstance(message, AIMessage) and message.id == "duplicate-boundary")
    assert calls == [*[f"prior-{index}" for index in range(9)], "boundary-0"]
    assert [call["args"]["query"] for call in boundary.tool_calls] == ["boundary-0"]
    assert [raw["function"]["arguments"] for raw in boundary.additional_kwargs["tool_calls"]] == ['{"query":"boundary-0"}']


def test_invalid_calls_survive_ambiguous_valid_occurrence_filtering() -> None:
    calls: list[str] = []

    @tool
    def web_search(query: str) -> str:
        """Search one query."""

        calls.append(query)
        return f"result:{query}"

    responses = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {"query": f"prior-{index}"},
                    "id": f"prior-{index}",
                }
            ],
        )
        for index in range(9)
    ]
    raw_valid = [
        {
            "id": "duplicate-id",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": f'{{"query":"boundary-{index}"}}',
            },
        }
        for index in range(3)
    ]
    raw_invalid = {
        "id": "invalid-id",
        "type": "function",
        "function": {"name": "web_search", "arguments": "{"},
    }
    responses.extend(
        [
            AIMessage(
                id="invalid-boundary",
                content="",
                additional_kwargs={
                    "tool_calls": [*raw_valid, raw_invalid],
                },
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": f"boundary-{index}"},
                        "id": "duplicate-id",
                    }
                    for index in range(3)
                ],
                invalid_tool_calls=[
                    {
                        "name": "web_search",
                        "args": "{",
                        "id": "invalid-id",
                        "error": "invalid arguments",
                    }
                ],
            ),
            AIMessage(id="final", content="done"),
        ]
    )
    agent = create_agent(
        model=_model(responses),
        tools=[web_search],
        middleware=[
            build_tool_call_control(
                _policy(),
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-invalid"),
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="research")]},
        context={"run_id": "run-invalid"},
    )

    boundary = next(message for message in result["messages"] if isinstance(message, AIMessage) and message.id == "invalid-boundary")
    assert calls == [*[f"prior-{index}" for index in range(9)], "boundary-0"]
    assert boundary.invalid_tool_calls == [
        {
            "name": "web_search",
            "args": "{",
            "id": "invalid-id",
            "error": "invalid arguments",
            "type": "invalid_tool_call",
        }
    ]
    assert boundary.additional_kwargs["tool_calls"] == [raw_invalid]


def test_checkpoint_replay_does_not_consume_or_observe_twice() -> None:
    observations: list[object] = []

    class _Observer:
        def observe(self, observation: object) -> None:
            observations.append(observation)

    middleware = build_tool_call_control(
        _policy(web_warn=2, web_hard=10),
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-replay"),
            observer=_Observer(),
        ),
    )
    proposal = AIMessage(
        id="proposal-replay",
        content="",
        tool_calls=[
            {
                "name": "web_search",
                "args": {"query": f"query-{index}"},
                "id": "reused-tool-call-id",
            }
            for index in range(3)
        ],
    )
    input_state = {
        "messages": [HumanMessage(content="research"), proposal],
    }

    first = middleware.after_model(input_state, Runtime(context={}))
    assert first is not None
    replay_state = {
        "messages": [input_state["messages"][0], first["messages"][-1]],
        TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
    }
    replay = middleware.after_model(replay_state, Runtime(context={}))

    assert replay is not None
    assert replay[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {"web_search": 3}
    assert len(observations) == 1
    assert observations[0].reason_code == "tool_budget_warning"


def test_checkpoint_replay_of_a_truncated_batch_keeps_the_same_occurrence() -> None:
    middleware = build_tool_call_control(
        _policy(web_warn=6, web_hard=10),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-truncated-replay"),
        ),
    )
    initialized = middleware.before_agent({}, Runtime(context={}))
    assert initialized is not None
    prior_facts = dict(initialized[TOOL_CALL_CONTROL_STATE_KEY])
    prior_facts["admitted_counts"] = {"web_search": 9}
    proposal = AIMessage(
        id="proposal-truncated",
        content="",
        tool_calls=[
            {
                "name": "web_search",
                "args": {"query": f"query-{index}"},
                "id": f"call-{index}",
            }
            for index in range(3)
        ],
    )

    first = middleware.after_model(
        {
            "messages": [proposal],
            TOOL_CALL_CONTROL_STATE_KEY: prior_facts,
        },
        Runtime(context={}),
    )

    assert first is not None
    post_control = first["messages"][0]
    assert [call["id"] for call in post_control.tool_calls] == ["call-0"]
    assert first[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {
        "web_search": 10,
    }

    replay = middleware.after_model(
        {
            "messages": [post_control],
            TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
        },
        Runtime(context={}),
    )

    assert replay is not None
    assert [call["id"] for call in replay["messages"][0].tool_calls] == ["call-0"]
    assert replay[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {
        "web_search": 10,
    }


def test_repeated_call_hard_limit_runs_one_tool_free_finalization() -> None:
    calls: list[str] = []
    observations: list[object] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return f"result:{value}"

    class _Observer:
        def observe(self, observation: object) -> None:
            observations.append(observation)

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=3,
            hard_limit=5,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=100, hard_limit=101),
            tools={},
        ),
    )
    responses = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {"value": "same"},
                    "id": f"call-{index}",
                }
            ],
        )
        for index in range(5)
    ]
    responses.append(AIMessage(id="final", content="finalized from collected evidence"))
    model = _model(responses)
    middleware = build_tool_call_control(
        policy,
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-loop"),
            observer=_Observer(),
        ),
    )
    agent = create_agent(
        model=model,
        tools=[lookup],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up")]},
        context={"run_id": "execution-loop"},
    )

    assert calls == ["same", "same", "same", "same"]
    assert result["messages"][-1].content == "finalized from collected evidence"
    replaced = next(message for message in result["messages"] if isinstance(message, AIMessage) and message.id == "proposal-4")
    assert replaced.additional_kwargs[TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY] is True
    assert replaced.tool_calls == []
    assert len(model.seen_messages) == 6
    assert model.bound_tool_names == [["lookup"]] * 5
    assert middleware.consume_stop_reason("execution-loop") == "loop_capped"
    assert middleware.consume_stop_reason("execution-loop") is None
    assert [observation.reason_code for observation in observations] == [
        "repeated_call_warning",
        "repeated_call_limit",
    ]


def test_repeated_call_identity_is_independent_of_batch_permutation() -> None:
    calls: list[str] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return value

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=3,
            hard_limit=5,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=100, hard_limit=101),
            tools={},
        ),
    )
    responses = []
    for index in range(5):
        values = ("alpha", "beta") if index % 2 == 0 else ("beta", "alpha")
        responses.append(
            AIMessage(
                id=f"proposal-{index}",
                content="",
                tool_calls=[
                    {
                        "name": "lookup",
                        "args": {"value": value},
                        "id": f"call-{index}-{offset}",
                    }
                    for offset, value in enumerate(values)
                ],
            )
        )
    responses.append(AIMessage(id="final", content="complete"))
    middleware = build_tool_call_control(
        policy,
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-permutation"),
        ),
    )
    agent = create_agent(
        model=_model(responses),
        tools=[lookup],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look up both values repeatedly")]},
        context={"run_id": "execution-permutation"},
    )

    assert calls.count("alpha") == 4
    assert calls.count("beta") == 4
    assert result["messages"][-1].content == "complete"
    assert middleware.consume_stop_reason("execution-permutation") == "loop_capped"


def test_web_search_auxiliary_arguments_are_part_of_repeated_call_identity() -> None:
    calls: list[tuple[str, int]] = []

    @tool
    def web_search(query: str, max_results: int) -> str:
        """Search one query with an explicit result limit."""

        calls.append((query, max_results))
        return f"result:{query}:{max_results}"

    middleware = build_tool_call_control(
        ResolvedToolCallControlPolicy(
            repeated_calls=RepeatedCallPolicy(
                warn_threshold=3,
                hard_limit=5,
                window_size=20,
            ),
            tool_budget=ResolvedToolCallBudgetPolicy(
                default=ToolCallLimit(warn_threshold=100, hard_limit=101),
                tools={},
            ),
        ),
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-web-search-arguments"),
        ),
    )
    responses = [
        AIMessage(
            id=f"proposal-{max_results}",
            content="",
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {
                        "query": "Agent history",
                        "max_results": max_results,
                    },
                    "id": f"call-{max_results}",
                }
            ],
        )
        for max_results in range(1, 6)
    ]
    responses.append(AIMessage(id="final", content="complete"))
    agent = create_agent(
        model=_model(responses),
        tools=[web_search],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="vary the result limit")]},
        context={"run_id": "execution-web-search-arguments"},
    )

    assert calls == [("Agent history", max_results) for max_results in range(1, 6)]
    assert result["messages"][-1].content == "complete"
    assert middleware.consume_stop_reason("execution-web-search-arguments") is None


def test_ls_auxiliary_arguments_are_part_of_repeated_call_identity() -> None:
    calls: list[tuple[str, int]] = []

    @tool
    def ls(path: str, depth: int) -> str:
        """List one path to an explicit depth."""

        calls.append((path, depth))
        return f"listing:{path}:{depth}"

    middleware = build_tool_call_control(
        ResolvedToolCallControlPolicy(
            repeated_calls=RepeatedCallPolicy(
                warn_threshold=3,
                hard_limit=5,
                window_size=20,
            ),
            tool_budget=ResolvedToolCallBudgetPolicy(
                default=ToolCallLimit(warn_threshold=100, hard_limit=101),
                tools={},
            ),
        ),
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-ls-arguments"),
        ),
    )
    responses = [
        AIMessage(
            id=f"proposal-{depth}",
            content="",
            tool_calls=[
                {
                    "name": "ls",
                    "args": {"path": ".", "depth": depth},
                    "id": f"call-{depth}",
                }
            ],
        )
        for depth in range(1, 6)
    ]
    responses.append(AIMessage(id="final", content="complete"))
    agent = create_agent(
        model=_model(responses),
        tools=[ls],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="vary the listing depth")]},
        context={"run_id": "execution-ls-arguments"},
    )

    assert calls == [(".", depth) for depth in range(1, 6)]
    assert result["messages"][-1].content == "complete"
    assert middleware.consume_stop_reason("execution-ls-arguments") is None


@pytest.mark.parametrize("argument_name", ["query", "url", "payload"])
def test_different_tool_arguments_are_productive_progress(
    argument_name: str,
) -> None:
    calls: list[tuple[str | None, str | None, str | None]] = []

    @tool
    def probe(
        query: str | None = None,
        url: str | None = None,
        payload: str | None = None,
    ) -> str:
        """Record one distinct tool request."""

        calls.append((query, url, payload))
        return "observed"

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=3,
            hard_limit=5,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=100, hard_limit=101),
            tools={},
        ),
    )
    middleware = build_tool_call_control(
        policy,
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope(
                f"execution-distinct-{argument_name}",
            ),
        ),
    )
    responses = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "probe",
                    "args": {argument_name: f"value-{index}"},
                    "id": f"call-{index}",
                }
            ],
        )
        for index in range(5)
    ]
    responses.append(AIMessage(id="final", content="complete"))
    agent = create_agent(
        model=_model(responses),
        tools=[probe],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="make five distinct requests")]},
        context={"run_id": f"execution-distinct-{argument_name}"},
    )

    assert len(calls) == 5
    assert result["messages"][-1].content == "complete"
    assert middleware.consume_stop_reason(f"execution-distinct-{argument_name}") is None


def test_fresh_authenticated_reads_and_distinct_writes_are_progress() -> None:
    files = {"report.md": "0"}

    @tool
    def read_file(path: str) -> str:
        """Read one text file."""

        return files[path]

    @tool
    def str_replace(path: str, old_str: str, new_str: str) -> str:
        """Replace one exact string in a text file."""

        assert files[path] == old_str
        files[path] = new_str
        return "updated"

    responses: list[AIMessage] = []
    for version in range(5):
        responses.extend(
            [
                AIMessage(
                    id=f"read-proposal-{version}",
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "report.md"},
                            "id": f"read-{version}",
                        }
                    ],
                ),
                AIMessage(
                    id=f"write-proposal-{version}",
                    content="",
                    tool_calls=[
                        {
                            "name": "str_replace",
                            "args": {
                                "path": "report.md",
                                "old_str": str(version),
                                "new_str": str(version + 1),
                            },
                            "id": f"write-{version}",
                        }
                    ],
                ),
            ]
        )
    responses.append(AIMessage(id="final", content="all revisions complete"))
    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=3,
            hard_limit=5,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=100, hard_limit=101),
            tools={},
        ),
    )
    middleware = build_tool_call_control(
        policy,
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-read-write"),
        ),
    )
    agent = create_agent(
        model=_model(responses),
        tools=[read_file, str_replace],
        middleware=[
            ReadBeforeWriteMiddleware(
                content_reader=lambda _runtime, path: files[path],
            ),
            middleware,
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="apply five verified revisions")]},
        context={"run_id": "execution-read-write"},
    )

    assert files["report.md"] == "5"
    assert result["messages"][-1].content == "all revisions complete"
    assert middleware.consume_stop_reason("execution-read-write") is None


def test_disabled_repetition_keeps_tool_budget_enforcement_active() -> None:
    calls: list[str] = []
    observations: list[object] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return f"result:{value}"

    class _Observer:
        def observe(self, observation: object) -> None:
            observations.append(observation)

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=2),
            tools={},
        ),
    )
    responses = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {"value": "same"},
                    "id": f"call-{index}",
                }
            ],
        )
        for index in range(3)
    ]
    responses.append(AIMessage(id="final", content="budget-limited answer"))
    agent = create_agent(
        model=_model(responses),
        tools=[lookup],
        middleware=[
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-budget-only"),
                    observer=_Observer(),
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up repeatedly")]},
        context={"run_id": "run-budget-only"},
    )

    assert calls == ["same", "same"]
    assert result["messages"][-1].content == "budget-limited answer"
    reason_codes = [observation.reason_code for observation in observations]
    assert reason_codes
    assert set(reason_codes) <= {
        "tool_budget_warning",
        "tool_budget_exhausted",
    }
    assert "tool_budget_exhausted" in reason_codes


def test_private_state_rejects_a_different_frozen_policy() -> None:
    proposal = AIMessage(
        id="proposal-policy",
        content="",
        tool_calls=[
            {
                "name": "web_search",
                "args": {"query": "one"},
                "id": "call-policy",
            }
        ],
    )
    original = build_tool_call_control(
        _policy(web_warn=6, web_hard=10),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-policy"),
        ),
    )
    first = original.after_model(
        {"messages": [HumanMessage(content="research"), proposal]},
        Runtime(context={}),
    )
    assert first is not None
    changed = build_tool_call_control(
        _policy(web_warn=12, web_hard=20),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-policy"),
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="tool_call_control_state_invalid: policy or binding mismatch",
    ):
        changed.after_model(
            {
                "messages": [
                    HumanMessage(content="research"),
                    AIMessage(
                        id="proposal-policy-2",
                        content="",
                        tool_calls=[
                            {
                                "name": "web_search",
                                "args": {"query": "two"},
                                "id": "call-policy-2",
                            }
                        ],
                    ),
                ],
                TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
            },
            Runtime(context={}),
        )


def test_loop_finalization_rejects_and_suppresses_another_tool_proposal() -> None:
    calls: list[str] = []
    stop_recorder = RunSemanticStopRecorder()

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return f"result:{value}"

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=100, hard_limit=101),
            tools={},
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal-1",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(
                    id="proposal-2",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-2",
                        }
                    ],
                ),
                AIMessage(
                    id="adversarial-3",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "must-not-run"},
                            "id": "call-3",
                        }
                    ],
                ),
            ]
        ),
        tools=[lookup],
        middleware=[
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-adversarial"),
                ),
            )
        ],
    )

    with pytest.raises(
        RuntimeError,
        match=("tool_call_control_loop_finalization_failed: model attempted another tool call"),
    ):
        agent.invoke(
            {"messages": [HumanMessage(content="look it up")]},
            context={
                "run_id": "run-adversarial",
                RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER: stop_recorder,
            },
        )

    assert calls == ["same"]
    assert stop_recorder.reason == "loop_capped"
    assert stop_recorder.suppressed_ai_message_ids == (
        "proposal-2",
        "adversarial-3",
    )


def test_distinct_exhausted_proposals_have_distinct_observation_ids() -> None:
    observations: list[object] = []

    class _Observer:
        def observe(self, observation: object) -> None:
            observations.append(observation)

    middleware = build_tool_call_control(
        ResolvedToolCallControlPolicy(
            repeated_calls=RepeatedCallPolicy(
                enabled=False,
                warn_threshold=1,
                hard_limit=2,
                window_size=20,
            ),
            tool_budget=ResolvedToolCallBudgetPolicy(
                default=ToolCallLimit(warn_threshold=1, hard_limit=1),
                tools={},
            ),
        ),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-observations"),
            observer=_Observer(),
        ),
    )
    human = HumanMessage(content="research")
    proposals = [
        AIMessage(
            id=f"proposal-{index}",
            content="",
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {"query": f"query-{index}"},
                    "id": "reused-id",
                }
            ],
        )
        for index in range(3)
    ]

    first = middleware.after_model(
        {"messages": [human, proposals[0]]},
        Runtime(context={}),
    )
    assert first is not None
    second = middleware.after_model(
        {
            "messages": [human, proposals[0], proposals[1]],
            TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
        },
        Runtime(context={}),
    )
    assert second is not None
    third = middleware.after_model(
        {
            "messages": [human, proposals[0], proposals[1], proposals[2]],
            TOOL_CALL_CONTROL_STATE_KEY: second[TOOL_CALL_CONTROL_STATE_KEY],
        },
        Runtime(context={}),
    )

    assert third is not None
    assert len(observations) == 3
    assert len({observation.observation_id for observation in observations}) == 3


@pytest.mark.asyncio
async def test_async_budget_path_matches_exact_batch_enforcement() -> None:
    calls: list[str] = []

    @tool
    async def web_search(query: str) -> str:
        """Search one query."""

        calls.append(query)
        return f"result:{query}"

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=2),
            tools={},
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal-1",
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "prior"},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(
                    id="boundary",
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "admitted"},
                            "id": "call-2",
                        },
                        {
                            "name": "web_search",
                            "args": {"query": "rejected"},
                            "id": "call-3",
                        },
                    ],
                ),
                AIMessage(id="final", content="async complete"),
            ]
        ),
        tools=[web_search],
        middleware=[
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="subagent",
                    scope=FixedToolCallControlScope("async-task"),
                ),
            )
        ],
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="research")]},
        context={"run_id": "async-task"},
    )

    assert calls == ["prior", "admitted"]
    assert result["messages"][-1].content == "async complete"


def test_cached_graph_resets_budget_for_each_explicit_invocation_scope() -> None:
    calls: list[str] = []
    observations: list[object] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return f"result:{value}"

    class _Observer:
        def observe(self, observation: object) -> None:
            observations.append(observation)

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=1),
            tools={},
        ),
    )
    model = _model(
        [
            AIMessage(
                id="invocation-a-proposal",
                content="",
                tool_calls=[
                    {
                        "name": "lookup",
                        "args": {"value": "a"},
                        "id": "reused-id",
                    }
                ],
            ),
            AIMessage(id="invocation-a-final", content="a complete"),
            AIMessage(
                id="invocation-b-proposal",
                content="",
                tool_calls=[
                    {
                        "name": "lookup",
                        "args": {"value": "b"},
                        "id": "reused-id",
                    }
                ],
            ),
            AIMessage(id="invocation-b-final", content="b complete"),
        ]
    )
    agent = create_agent(
        model=model,
        tools=[lookup],
        middleware=[
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="lead",
                    scope=PerInvocationToolCallControlScope(),
                    observer=_Observer(),
                ),
            )
        ],
    )

    first = agent.invoke(
        {"messages": [HumanMessage(content="first invocation")]},
        context={TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "invocation-a"},
    )
    second = agent.invoke(
        {"messages": [HumanMessage(content="second invocation")]},
        context={TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "invocation-b"},
    )

    assert first["messages"][-1].content == "a complete"
    assert second["messages"][-1].content == "b complete"
    assert calls == ["a", "b"]
    assert [observation.scope_id for observation in observations] == [
        "invocation-a",
        "invocation-b",
    ]


def test_fixed_execution_scope_keeps_budget_across_graph_turns() -> None:
    middleware = build_tool_call_control(
        ResolvedToolCallControlPolicy(
            repeated_calls=RepeatedCallPolicy(
                enabled=False,
                warn_threshold=1,
                hard_limit=2,
                window_size=20,
            ),
            tool_budget=ResolvedToolCallBudgetPolicy(
                default=ToolCallLimit(warn_threshold=1, hard_limit=1),
                tools={},
            ),
        ),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-fixed"),
        ),
    )
    first_proposal = AIMessage(
        id="turn-1",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"value": "first"},
                "id": "call-1",
            }
        ],
    )
    first = middleware.after_model(
        {"messages": [HumanMessage(content="turn one"), first_proposal]},
        Runtime(context={}),
    )
    assert first is not None
    second_proposal = AIMessage(
        id="turn-2",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"value": "second"},
                "id": "call-2",
            }
        ],
    )
    second = middleware.after_model(
        {
            "messages": [
                HumanMessage(content="turn one"),
                first_proposal,
                HumanMessage(content="turn two"),
                second_proposal,
            ],
            TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
        },
        Runtime(context={}),
    )

    assert second is not None
    assert second[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {"lookup": 1}
    assert second["messages"][-1].tool_calls == []


def test_missing_explicit_invocation_scope_fails_before_model_call() -> None:
    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        return value

    model = _model([AIMessage(id="must-not-run", content="unexpected")])
    agent = create_agent(
        model=model,
        tools=[lookup],
        middleware=[
            build_tool_call_control(
                _policy(),
                ToolCallControlBinding(
                    role="lead",
                    scope=PerInvocationToolCallControlScope(),
                ),
            )
        ],
    )

    with pytest.raises(
        RuntimeError,
        match=("tool_call_control_state_invalid: explicit invocation scope missing"),
    ):
        agent.invoke({"messages": [HumanMessage(content="research")]})

    assert model.seen_messages == []


def test_compacted_checkpoint_replay_uses_server_receipt_not_message_index() -> None:
    middleware = build_tool_call_control(
        _policy(web_warn=2, web_hard=10),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-compacted"),
        ),
    )
    proposal = AIMessage(
        id=None,
        content="",
        tool_calls=[
            {
                "name": "web_search",
                "args": {"query": f"query-{index}"},
                "id": "reused-id",
            }
            for index in range(3)
        ],
    )
    first = middleware.after_model(
        {"messages": [HumanMessage(content="research"), proposal]},
        Runtime(context={}),
    )
    assert first is not None
    stamped = first["messages"][-1]
    assert stamped.additional_kwargs[TOOL_CALL_CONTROL_RECEIPT_KEY]

    replay = middleware.after_model(
        {
            "messages": [stamped],
            TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
        },
        Runtime(context={}),
    )

    assert replay is not None
    assert replay[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {"web_search": 3}


def test_compacted_checkpoint_counts_new_unstamped_proposal_at_reused_index() -> None:
    middleware = build_tool_call_control(
        _policy(web_warn=9, web_hard=10),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-compacted-new-proposal"),
        ),
    )
    proposal = AIMessage(
        id=None,
        content="",
        tool_calls=[
            {
                "name": "web_search",
                "args": {"query": "same-query"},
                "id": "reused-id",
            }
        ],
    )
    first = middleware.after_model(
        {"messages": [HumanMessage(content="first context"), proposal]},
        Runtime(context={}),
    )
    assert first is not None
    stamped = first["messages"][-1]

    replay = middleware.after_model(
        {
            "messages": [stamped],
            TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
        },
        Runtime(context={}),
    )
    assert replay is not None
    assert replay[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {
        "web_search": 1,
    }
    assert len(replay[TOOL_CALL_CONTROL_STATE_KEY]["recent_fingerprints"]) == 1

    fresh = middleware.after_model(
        {
            "messages": [
                HumanMessage(content="compacted context"),
                proposal.model_copy(deep=True),
            ],
            TOOL_CALL_CONTROL_STATE_KEY: replay[TOOL_CALL_CONTROL_STATE_KEY],
        },
        Runtime(context={}),
    )

    assert fresh is not None
    assert fresh[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {
        "web_search": 2,
    }
    assert len(fresh[TOOL_CALL_CONTROL_STATE_KEY]["recent_fingerprints"]) == 2
    assert fresh["messages"][-1].additional_kwargs[TOOL_CALL_CONTROL_RECEIPT_KEY] != stamped.additional_kwargs[TOOL_CALL_CONTROL_RECEIPT_KEY]


def test_task_delegation_is_not_charged_to_the_general_tool_budget() -> None:
    calls: list[str] = []

    @tool
    def task(description: str) -> str:
        """Delegate one bounded task."""

        calls.append(description)
        return f"completed:{description}"

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=1),
            tools={},
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="task-1",
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"description": "first"},
                            "id": "task-call-1",
                        }
                    ],
                ),
                AIMessage(
                    id="task-2",
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"description": "second"},
                            "id": "task-call-2",
                        }
                    ],
                ),
                AIMessage(id="final", content="delegations complete"),
            ]
        ),
        tools=[task],
        middleware=[
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-task-policy"),
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="delegate twice")]},
        context={"run_id": "run-task-policy"},
    )

    assert calls == ["first", "second"]
    assert result["messages"][-1].content == "delegations complete"


def test_observer_failure_does_not_change_budget_enforcement() -> None:
    calls: list[str] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return value

    class _FailingObserver:
        def observe(self, observation: object) -> None:
            del observation
            raise RuntimeError("observer unavailable")

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=1),
            tools={},
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "admitted"},
                            "id": "call",
                        }
                    ],
                ),
                AIMessage(id="final", content="complete"),
            ]
        ),
        tools=[lookup],
        middleware=[
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-observer"),
                    observer=_FailingObserver(),
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up")]},
        context={"run_id": "run-observer"},
    )

    assert calls == ["admitted"]
    assert result["messages"][-1].content == "complete"


def test_observer_failure_does_not_change_repeated_call_enforcement() -> None:
    calls: list[str] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return value

    class _FailingObserver:
        def observe(self, observation: object) -> None:
            del observation
            raise RuntimeError("observer unavailable")

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=1,
            hard_limit=2,
            window_size=2,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=100, hard_limit=101),
            tools={},
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "admitted"},
                            "id": "call",
                        }
                    ],
                ),
                AIMessage(id="final", content="complete"),
            ]
        ),
        tools=[lookup],
        middleware=[
            build_tool_call_control(
                policy,
                ToolCallControlBinding(
                    role="lead",
                    scope=FixedToolCallControlScope("run-repeated-observer"),
                    observer=_FailingObserver(),
                ),
            )
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up")]},
        context={"run_id": "run-repeated-observer"},
    )

    assert calls == ["admitted"]
    assert result["messages"][-1].content == "complete"


def test_reason_code_contract_is_closed() -> None:
    assert set(get_args(ToolCallControlReasonCode)) == {
        "repeated_call_warning",
        "repeated_call_limit",
        "tool_budget_warning",
        "tool_budget_exhausted",
    }


@pytest.mark.parametrize(
    ("workload_profile", "lead_web", "subagent_web"),
    [
        ("interactive", (6, 10), (6, 10)),
        ("research", (20, 30), (12, 20)),
    ],
)
def test_default_graph_profile_matches_policy_v4_defaults(
    workload_profile: str,
    lead_web: tuple[int, int],
    subagent_web: tuple[int, int],
) -> None:
    profile = default_graph_tool_call_control_profile(workload_profile)  # type: ignore[arg-type]

    assert profile.workload_profile == workload_profile
    assert (
        profile.lead.tool_budget.limit_for("web_search").warn_threshold,
        profile.lead.tool_budget.limit_for("web_search").hard_limit,
    ) == lead_web
    assert (
        profile.subagent.tool_budget.limit_for("web_fetch").warn_threshold,
        profile.subagent.tool_budget.limit_for("web_fetch").hard_limit,
    ) == subagent_web
    assert profile.lead.tool_budget.default == ToolCallLimit(30, 50)
    assert profile.subagent.tool_budget.limit_for("recall_memory") == ToolCallLimit(
        6,
        10,
    )
    assert profile.lead.tool_budget.limit_for("inspect_image") == ToolCallLimit(
        6,
        MAX_VISION_CALLS_PER_RUN,
    )
    assert profile.lead.repeated_calls == RepeatedCallPolicy(
        warn_threshold=3,
        hard_limit=5,
        window_size=20,
    )


def test_inspect_image_budget_cannot_exceed_the_dispatch_technical_cap() -> None:
    budget = ResolvedToolCallBudgetPolicy(
        default=ToolCallLimit(warn_threshold=30, hard_limit=50),
        tools={
            "inspect_image": ToolCallLimit(
                warn_threshold=6,
                hard_limit=50,
            )
        },
    )

    assert budget.limit_for("inspect_image") == ToolCallLimit(
        warn_threshold=6,
        hard_limit=8,
    )
    middleware = build_tool_call_control(
        ResolvedToolCallControlPolicy(
            repeated_calls=RepeatedCallPolicy(
                enabled=False,
                warn_threshold=1,
                hard_limit=2,
                window_size=2,
            ),
            tool_budget=budget,
        ),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-inspect-cap"),
        ),
    )
    update = middleware.after_model(
        {
            "messages": [
                AIMessage(
                    id="inspect-batch",
                    content="",
                    tool_calls=[
                        {
                            "name": "inspect_image",
                            "args": {"image_path": f"uploads/{index}.png"},
                            "id": f"inspect-{index}",
                        }
                        for index in range(9)
                    ],
                )
            ]
        },
        Runtime(context={}),
    )

    assert update is not None
    assert len(update["messages"][0].tool_calls) == MAX_VISION_CALLS_PER_RUN
    assert update[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {
        "inspect_image": MAX_VISION_CALLS_PER_RUN,
    }


def test_inspect_image_budget_preserves_a_stricter_policy_limit() -> None:
    policy = ResolvedToolCallBudgetPolicy(
        default=ToolCallLimit(warn_threshold=30, hard_limit=50),
        tools={
            "inspect_image": ToolCallLimit(
                warn_threshold=3,
                hard_limit=5,
            )
        },
    )

    assert policy.limit_for("inspect_image") == ToolCallLimit(
        warn_threshold=3,
        hard_limit=5,
    )


def test_default_graph_profile_can_disable_repeated_call_enforcement() -> None:
    profile = default_graph_tool_call_control_profile(
        repeated_calls_enabled=False,
    )

    assert profile.lead.repeated_calls.enabled is False
    assert profile.subagent.repeated_calls.enabled is False


def test_subagent_tool_budget_exhaustion_records_additive_stop_reason() -> None:
    calls: list[str] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return value

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=1),
            tools={},
        ),
    )
    middleware = build_tool_call_control(
        policy,
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-budget"),
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "admitted"},
                            "id": "call",
                        }
                    ],
                ),
                AIMessage(id="final", content="complete with existing evidence"),
            ]
        ),
        tools=[lookup],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up")]},
        context={},
    )

    assert calls == ["admitted"]
    assert result["messages"][-1].content == "complete with existing evidence"
    assert middleware.consume_stop_reason("execution-budget") == ("tool_budget_capped")


def test_subagent_loop_stop_reason_wins_after_tool_budget_exhaustion() -> None:
    calls: list[str] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        calls.append(value)
        return value

    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            warn_threshold=1,
            hard_limit=2,
            window_size=20,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(warn_threshold=1, hard_limit=1),
            tools={},
        ),
    )
    middleware = build_tool_call_control(
        policy,
        ToolCallControlBinding(
            role="subagent",
            scope=FixedToolCallControlScope("execution-loop-priority"),
        ),
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal-1",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(
                    id="proposal-2",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-2",
                        }
                    ],
                ),
                AIMessage(id="final", content="loop-free finalization"),
            ]
        ),
        tools=[lookup],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up")]},
        context={},
    )

    assert calls == ["same"]
    assert result["messages"][-1].content == "loop-free finalization"
    assert middleware.consume_stop_reason("execution-loop-priority") == ("loop_capped")


def test_before_agent_resets_well_formed_state_for_a_new_invocation_scope() -> None:
    middleware = build_tool_call_control(
        ResolvedToolCallControlPolicy(
            repeated_calls=RepeatedCallPolicy(
                enabled=False,
                warn_threshold=1,
                hard_limit=2,
                window_size=20,
            ),
            tool_budget=ResolvedToolCallBudgetPolicy(
                default=ToolCallLimit(warn_threshold=1, hard_limit=2),
                tools={},
            ),
        ),
        ToolCallControlBinding(
            role="lead",
            scope=PerInvocationToolCallControlScope(),
        ),
    )
    invocation_a = Runtime(context={TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "run-a"})
    initial = middleware.before_agent({}, invocation_a)
    assert initial is not None
    proposal = middleware.after_model(
        {
            "messages": [
                HumanMessage(content="first Run"),
                AIMessage(
                    id="proposal-a",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "a"},
                            "id": "call-a",
                        }
                    ],
                ),
            ],
            TOOL_CALL_CONTROL_STATE_KEY: initial[TOOL_CALL_CONTROL_STATE_KEY],
        },
        invocation_a,
    )
    assert proposal is not None
    invocation_b = Runtime(context={TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "run-b"})

    reset = middleware.before_agent(
        {TOOL_CALL_CONTROL_STATE_KEY: proposal[TOOL_CALL_CONTROL_STATE_KEY]},
        invocation_b,
    )

    assert reset is not None
    assert reset[TOOL_CALL_CONTROL_STATE_KEY]["scope_id"] == "run-b"
    assert reset[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {}


def test_before_agent_resets_a_prior_scope_when_workload_policy_changes() -> None:
    interactive = default_graph_tool_call_control_profile("interactive")
    research = default_graph_tool_call_control_profile("research")
    prior_middleware = build_tool_call_control(
        interactive.lead,
        ToolCallControlBinding(
            role="lead",
            scope=PerInvocationToolCallControlScope(),
            workload_profile="interactive",
        ),
    )
    prior_runtime = Runtime(
        context={TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "invocation-a"},
    )
    prior = prior_middleware.before_agent({}, prior_runtime)
    assert prior is not None
    current_middleware = build_tool_call_control(
        research.lead,
        ToolCallControlBinding(
            role="lead",
            scope=PerInvocationToolCallControlScope(),
            workload_profile="research",
        ),
    )

    reset = current_middleware.before_agent(
        {TOOL_CALL_CONTROL_STATE_KEY: prior[TOOL_CALL_CONTROL_STATE_KEY]},
        Runtime(
            context={
                TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "invocation-b",
            },
        ),
    )

    assert reset is not None
    assert reset[TOOL_CALL_CONTROL_STATE_KEY]["scope_id"] == "invocation-b"
    assert reset[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {}


def test_before_agent_rejects_same_scope_policy_fingerprint_tampering() -> None:
    middleware = build_tool_call_control(
        _policy(),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-tampered"),
        ),
    )
    initialized = middleware.before_agent({}, Runtime(context={}))
    assert initialized is not None
    tampered = dict(initialized[TOOL_CALL_CONTROL_STATE_KEY])
    tampered["contract_fingerprint"] = "0" * 64

    with pytest.raises(
        RuntimeError,
        match="tool_call_control_state_invalid: policy or binding mismatch",
    ):
        middleware.before_agent(
            {TOOL_CALL_CONTROL_STATE_KEY: tampered},
            Runtime(context={}),
        )
