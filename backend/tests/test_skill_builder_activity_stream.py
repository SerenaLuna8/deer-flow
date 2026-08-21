from __future__ import annotations

import pytest

from app.shared_assets.skill_builder_activity_stream import (
    SkillBuilderActivityStreamBridge,
)
from app.shared_assets.skill_design_activity import SkillDesignActivityKind


class _Bridge:
    def __init__(self) -> None:
        self.frames: list[tuple[str, str, object]] = []

    async def publish(self, run_id: str, event: str, data: object) -> None:
        self.frames.append((run_id, event, data))

    async def publish_end(self, run_id: str) -> None:
        self.frames.append((run_id, "end", None))


class _Emitter:
    def __init__(self) -> None:
        self.events: list[tuple[SkillDesignActivityKind, dict[str, object], str | None]] = []

    async def append(
        self,
        kind: SkillDesignActivityKind,
        *,
        payload: dict[str, object] | None = None,
        source_event_id: str | None = None,
    ) -> None:
        self.events.append((kind, dict(payload or {}), source_event_id))


@pytest.mark.asyncio
async def test_stream_bridge_keeps_only_real_reasoning_and_safe_tool_lifecycle() -> None:
    raw = _Bridge()
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(raw, emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "ai",
                "content": "visible answer is not activity reasoning",
                "additional_kwargs": {
                    "reasoning_content": "真实思考",
                    "provider_response": "must-not-project",
                },
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "read_candidate_file",
                        "args": {
                            "path": "references/guide.md",
                            "secret": "must-not-project",
                        },
                    },
                    {"id": "call-2", "name": "bash", "args": {}},
                ],
            },
            {"langgraph_node": "agent"},
        ],
    )
    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "tool",
                "tool_call_id": "call-1",
                "name": "read_candidate_file",
                "content": ('{"path":"references/guide.md","file_size_bytes":42,"content":"must-not-project"}'),
            },
            {},
        ],
    )

    assert raw.frames[0][1] == "messages"
    assert emitter.events == [
        (
            SkillDesignActivityKind.REASONING,
            {"text": "真实思考"},
            None,
        ),
        (
            SkillDesignActivityKind.TOOL_STARTED,
            {
                "tool_call_id": "call-1",
                "tool_name": "read_candidate_file",
                "path": "references/guide.md",
            },
            "tool-started:call-1",
        ),
        (
            SkillDesignActivityKind.TOOL_COMPLETED,
            {
                "tool_call_id": "call-1",
                "tool_name": "read_candidate_file",
                "path": "references/guide.md",
                "size_bytes": 42,
            },
            "tool-completed:call-1",
        ),
    ]
    assert "must-not-project" not in repr(emitter.events)


@pytest.mark.asyncio
async def test_stream_bridge_does_not_invent_reasoning() -> None:
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [{"type": "ai", "content": "普通回答"}, {}],
    )

    assert emitter.events == []


@pytest.mark.parametrize(
    ("reasoning", "expected_fragment"),
    [
        (
            "You are ActWeave's internal Skill Builder Agent. Mandatory boundaries follow.",
            "ActWeave's internal Skill Builder Agent",
        ),
        (
            "You author a candidate Skill package through a governed, durable Run. P0_SKILL_PROMPT_OVERLAP_CANARY",
            "P0_SKILL_PROMPT_OVERLAP_CANARY",
        ),
        (
            "Candidate file SKILL.md body: Payroll approval threshold is 987654 yuan.",
            "Payroll approval threshold is 987654 yuan",
        ),
        (
            f"draft checksum: {'b' * 64}",
            "b" * 64,
        ),
        (
            'Tool schema: {"name":"upsert_candidate_file","parameters":{"type":"object","properties":{"content":{"type":"string"}}}}',
            '"properties":{"content":{"type":"string"}}',
        ),
        (
            "Internal error: sqlalchemy.exc.IntegrityError from uq_skill_design_activities_source",
            "uq_skill_design_activities_source",
        ),
        (
            '{"path":"SKILL.md","content":"---\\nname: p0-skill\\n---\\nP0_SKILL_BODY_CANARY","mode":"replace"}',
            "P0_SKILL_BODY_CANARY",
        ),
        (
            '{"path":"SKILL.md","mode":"replace","expected_file_size_bytes":0,"expected_file_sha256":null}',
            "expected_file_sha256",
        ),
        (
            "Security and data boundary:\nP0_SKILL_SECURITY_BOUNDARY_CANARY",
            "P0_SKILL_SECURITY_BOUNDARY_CANARY",
        ),
        (
            "Mandatory boundaries:\nP0_SKILL_MANDATORY_BOUNDARY_CANARY",
            "P0_SKILL_MANDATORY_BOUNDARY_CANARY",
        ),
        (
            "---\nname: p0-private-skill\ndescription: P0_SKILL_YAML_CANARY\n---\n# Instructions",
            "P0_SKILL_YAML_CANARY",
        ),
        (
            'Calling search_available_skills with {"query":"P0_TOOL_ARGS_CANARY"}',
            "P0_TOOL_ARGS_CANARY",
        ),
        (
            "TOOL_EXECUTION_FAILED: P0_INTERNAL_TOOL_FAILURE_CANARY",
            "P0_INTERNAL_TOOL_FAILURE_CANARY",
        ),
        (
            "Authorization: Bearer p0SuperSecretBearerToken12345",
            "p0SuperSecretBearerToken12345",
        ),
        (
            "api_key=p0SecretApiKeyValue12345",
            "p0SecretApiKeyValue12345",
        ),
        (
            "The password is huntertwo P0_PASSWORD_SENTENCE_CANARY",
            "P0_PASSWORD_SENTENCE_CANARY",
        ),
        (
            "Provider error: status 429 quota exceeded P0_PROVIDER_ERROR_CANARY",
            "P0_PROVIDER_ERROR_CANARY",
        ),
        (
            "postgresql://owner:private-password@db.example/private P0_CREDENTIAL_URL_CANARY",
            "P0_CREDENTIAL_URL_CANARY",
        ),
        (
            "Session 77777777-7777-4777-8777-777777777777 is private P0_UUID_CANARY",
            "P0_UUID_CANARY",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nP0_PRIVATE_KEY_CANARY",
            "P0_PRIVATE_KEY_CANARY",
        ),
        (
            "base64_data: QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=",
            "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=",
        ),
        (
            "storage_locator=project-private://bucket/P0_STORAGE_LOCATOR_CANARY",
            "P0_STORAGE_LOCATOR_CANARY",
        ),
        (
            "We need respond JSON. Need follow phases. The interview history is empty. P0_SKILL_LIVE_PLAN_CANARY",
            "P0_SKILL_LIVE_PLAN_CANARY",
        ),
        (
            "The user is now instructing me to recreate the package. The draft checksum is null. P0_SKILL_USER_INSTRUCTION_PLAN_CANARY",
            "P0_SKILL_USER_INSTRUCTION_PLAN_CANARY",
        ),
    ],
    ids=(
        "system-prompt",
        "system-prompt-verbatim-overlap",
        "candidate-file-body",
        "checksum",
        "tool-schema",
        "internal-error",
        "raw-upsert-args-body",
        "raw-upsert-args-cas",
        "security-boundary",
        "mandatory-boundaries",
        "skill-frontmatter",
        "tool-call-with-args",
        "tool-execution-failed",
        "bearer-token",
        "api-key",
        "password-sentence",
        "provider-error",
        "credential-url",
        "uuid",
        "private-key",
        "base64-payload",
        "storage-locator",
        "live-provider-self-planning",
        "live-provider-user-instruction-plan",
    ),
)
@pytest.mark.asyncio
async def test_stream_bridge_projects_all_provider_reasoning_content(
    reasoning: str,
    expected_fragment: str,
) -> None:
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "ai",
                "content": "ordinary answer",
                "additional_kwargs": {
                    "reasoning_content": reasoning,
                },
            },
            {},
        ],
    )

    public_reasoning = [str(payload["text"]) for kind, payload, _source_event_id in emitter.events if kind is SkillDesignActivityKind.REASONING]
    assert public_reasoning == [reasoning]
    assert expected_fragment in "".join(public_reasoning)


@pytest.mark.parametrize(
    ("reasoning_chunks", "expected_fragment"),
    [
        (
            ("system pro", "mpt: P0_SKILL_SPLIT_SYSTEM_CANARY"),
            "P0_SKILL_SPLIT_SYSTEM_CANARY",
        ),
        (
            (
                '{"path":"SKILL.md","con',
                'tent":"---\\nname: p0-skill\\n---\\nP0_SKILL_SPLIT_BODY_CANARY"}',
            ),
            "P0_SKILL_SPLIT_BODY_CANARY",
        ),
        (
            (
                '{"path":"SKILL.md","mode":"replace","expected_file_sha',
                '256":null,"note":"P0_SKILL_SPLIT_CAS_CANARY"}',
            ),
            "P0_SKILL_SPLIT_CAS_CANARY",
        ),
        (
            (
                "We need respond JSON. Need follow ",
                "phases. The interview-history is empty. P0_SKILL_SPLIT_LIVE_PLAN_CANARY",
            ),
            "P0_SKILL_SPLIT_LIVE_PLAN_CANARY",
        ),
    ],
    ids=(
        "system-prompt",
        "raw-upsert-args-body",
        "raw-upsert-args-cas",
        "live-provider-self-planning",
    ),
)
@pytest.mark.asyncio
async def test_stream_bridge_projects_reasoning_split_across_messages(
    reasoning_chunks: tuple[str, str],
    expected_fragment: str,
) -> None:
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    for chunk in reasoning_chunks:
        await bridge.publish(
            "run-1",
            "messages",
            [
                {
                    "type": "ai",
                    "additional_kwargs": {"reasoning_content": chunk},
                },
                {},
            ],
        )

    public_reasoning = [str(payload["text"]) for kind, payload, _source_event_id in emitter.events if kind is SkillDesignActivityKind.REASONING]
    assert public_reasoning == list(reasoning_chunks)
    assert expected_fragment in "".join(public_reasoning)


@pytest.mark.asyncio
async def test_stream_bridge_preserves_benign_reasoning_chunk_order() -> None:
    chunks = ("先核对需求。", "再搜索 ", "可用引用", "，最后生成候选。")
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    for chunk in chunks:
        await bridge.publish(
            "run-1",
            "messages",
            [
                {
                    "type": "ai",
                    "additional_kwargs": {"reasoning_content": chunk},
                },
                {},
            ],
        )
    await bridge.publish_end("run-1")

    public_reasoning = "".join(str(payload["text"]) for kind, payload, _source_event_id in emitter.events if kind is SkillDesignActivityKind.REASONING)
    assert public_reasoning == "".join(chunks)


@pytest.mark.parametrize(
    "benign_reasoning",
    [
        "I will compare the checksum/digest before continuing.",
        "我会先比较校验和，再继续。",
    ],
    ids=("checksum-digest-terms", "chinese-checksum-term"),
)
@pytest.mark.asyncio
async def test_stream_bridge_keeps_benign_checksum_terminology(
    benign_reasoning: str,
) -> None:
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "ai",
                "additional_kwargs": {"reasoning_content": benign_reasoning},
            },
            {},
        ],
    )
    await bridge.publish_end("run-1")

    public_reasoning = "".join(str(payload["text"]) for kind, payload, _source_event_id in emitter.events if kind is SkillDesignActivityKind.REASONING)
    assert public_reasoning == benign_reasoning


@pytest.mark.asyncio
async def test_stream_bridge_projects_reasoning_at_every_chunk_boundary() -> None:
    reasoning = '{"path":"SKILL.md","content":"---\\nname: p0-private-skill\\n---\\nP0_ALL_CUTS_CANARY","mode":"replace"}'

    for cut in range(1, len(reasoning)):
        emitter = _Emitter()
        bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]
        chunks = (reasoning[:cut], reasoning[cut:])
        for chunk in chunks:
            await bridge.publish(
                "run-1",
                "messages",
                [
                    {
                        "type": "ai",
                        "additional_kwargs": {"reasoning_content": chunk},
                    },
                    {},
                ],
            )
        await bridge.publish_end("run-1")

        assert [payload for kind, payload, _source_event_id in emitter.events if kind is SkillDesignActivityKind.REASONING] == [
            {"text": chunks[0]},
            {"text": chunks[1]},
        ]


@pytest.mark.asyncio
async def test_stream_bridge_never_projects_catalog_references() -> None:
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "name": "read_skill_version",
                        "args": {"reference": "skill:project:private-name:v4"},
                    },
                    {
                        "id": "inspect-1",
                        "name": "inspect_mcp_tool",
                        "args": {"reference": "mcp:project:private-server:v2:secret"},
                    },
                ],
            },
            {},
        ],
    )
    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "tool",
                "tool_call_id": "read-1",
                "name": "read_skill_version",
                "status": "failed",
                "content": "must-not-project",
            },
            {},
        ],
    )
    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "tool",
                "tool_call_id": "inspect-1",
                "name": "inspect_mcp_tool",
                "content": ('{"reference":"mcp:project:private-server:v2:secret","server_name":"Public Server","tool_name":"Public Tool"}'),
            },
            {},
        ],
    )

    assert emitter.events == [
        (
            SkillDesignActivityKind.TOOL_STARTED,
            {"tool_call_id": "read-1", "tool_name": "read_skill_version"},
            "tool-started:read-1",
        ),
        (
            SkillDesignActivityKind.TOOL_STARTED,
            {"tool_call_id": "inspect-1", "tool_name": "inspect_mcp_tool"},
            "tool-started:inspect-1",
        ),
        (
            SkillDesignActivityKind.TOOL_FAILED,
            {"tool_call_id": "read-1", "tool_name": "read_skill_version"},
            "tool-failed:read-1",
        ),
        (
            SkillDesignActivityKind.TOOL_COMPLETED,
            {
                "tool_call_id": "inspect-1",
                "tool_name": "inspect_mcp_tool",
                "resource_name": "Public Server / Public Tool",
            },
            "tool-completed:inspect-1",
        ),
    ]
    assert "private" not in repr(emitter.events)
