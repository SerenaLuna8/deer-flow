from __future__ import annotations

import pytest

from app.channels.message_bus import MessageBus
from app.gateway.github.dispatcher import _is_self_event, fanout_event
from app.gateway.github.registry import GitHubAgentMatch
from deerflow.config.agents_config import (
    AgentConfig,
    GitHubAgentConfig,
    GitHubBinding,
    GitHubTriggerConfig,
)


def test_self_event_uses_explicit_bot_identity() -> None:
    github = GitHubAgentConfig(bot_login="deerflow-bot", bindings=[])

    assert _is_self_event(
        "issue_comment",
        {"sender": {"login": "deerflow-bot[bot]"}},
        "reviewer",
        github,
    )
    assert not _is_self_event(
        "issue_comment",
        {"sender": {"login": "third-party-bot[bot]"}},
        "reviewer",
        github,
    )


def _review_agent(
    *,
    comment_trigger: GitHubTriggerConfig | None = None,
    review_trigger: GitHubTriggerConfig | None = None,
) -> AgentConfig:
    triggers = {
        "pull_request_review_comment": comment_trigger or GitHubTriggerConfig(),
    }
    if review_trigger is not None:
        triggers["pull_request_review"] = review_trigger
    return AgentConfig(
        name="reviewer",
        github=GitHubAgentConfig(
            bindings=[
                GitHubBinding(
                    repo="a/b",
                    triggers=triggers,
                )
            ],
        ),
    )


def _review_comment_payload(
    *,
    review_id: int | None = 99,
    in_reply_to_id: int | None = None,
    body: str = "nit",
) -> dict:
    return {
        "action": "created",
        "pull_request": {"number": 7},
        "comment": {
            "body": body,
            "user": {"login": "alice"},
            "pull_request_review_id": review_id,
            "in_reply_to_id": in_reply_to_id,
        },
        "repository": {"full_name": "a/b"},
        "sender": {"login": "alice"},
    }


async def _fanout_review_comment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: AgentConfig,
    payload: dict,
) -> tuple[dict, MessageBus]:
    assert agent.github is not None
    comment_trigger = agent.github.bindings[0].triggers["pull_request_review_comment"]
    registry = {
        ("a/b", "pull_request_review_comment"): [
            GitHubAgentMatch(
                user_id="owner-a",
                agent=agent,
                trigger=comment_trigger,
            )
        ],
    }
    review_trigger = agent.github.bindings[0].triggers.get("pull_request_review")
    if review_trigger is not None:
        registry[("a/b", "pull_request_review")] = [
            GitHubAgentMatch(
                user_id="owner-a",
                agent=agent,
                trigger=review_trigger,
            )
        ]
    monkeypatch.setattr(
        "app.gateway.github.dispatcher.build_github_agent_registry",
        lambda: registry,
    )
    bus = MessageBus()
    result = await fanout_event(
        bus,
        "pull_request_review_comment",
        "delivery-1",
        payload,
    )
    return result, bus


@pytest.mark.asyncio
async def test_companion_review_comment_is_suppressed_per_dual_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, bus = await _fanout_review_comment(
        monkeypatch,
        agent=_review_agent(review_trigger=GitHubTriggerConfig()),
        payload=_review_comment_payload(),
    )

    assert result["fired_agents"] == []
    assert result["skipped"] == [{"agent": "reviewer", "reason": "redundant_review_comment"}]
    assert bus.inbound_queue.empty()


@pytest.mark.asyncio
async def test_review_comment_only_binding_keeps_companion_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, bus = await _fanout_review_comment(
        monkeypatch,
        agent=_review_agent(),
        payload=_review_comment_payload(),
    )

    assert result["fired_agents"] == ["reviewer"]
    assert not bus.inbound_queue.empty()


@pytest.mark.asyncio
async def test_review_thread_reply_is_never_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, bus = await _fanout_review_comment(
        monkeypatch,
        agent=_review_agent(review_trigger=GitHubTriggerConfig()),
        payload=_review_comment_payload(in_reply_to_id=123),
    )

    assert result["fired_agents"] == ["reviewer"]
    assert not bus.inbound_queue.empty()


@pytest.mark.asyncio
async def test_mention_gated_paired_review_does_not_cover_inline_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, bus = await _fanout_review_comment(
        monkeypatch,
        agent=_review_agent(
            review_trigger=GitHubTriggerConfig(
                require_mention=True,
                mention_login="reviewer",
            )
        ),
        payload=_review_comment_payload(body="@reviewer please inspect"),
    )

    assert result["fired_agents"] == ["reviewer"]
    assert not bus.inbound_queue.empty()


@pytest.mark.asyncio
async def test_paired_review_without_submitted_action_does_not_suppress_inline_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, bus = await _fanout_review_comment(
        monkeypatch,
        agent=_review_agent(
            review_trigger=GitHubTriggerConfig(actions=["dismissed"]),
        ),
        payload=_review_comment_payload(),
    )

    assert result["fired_agents"] == ["reviewer"]
    assert not bus.inbound_queue.empty()


@pytest.mark.asyncio
async def test_paired_submitted_review_suppresses_companion_inline_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, bus = await _fanout_review_comment(
        monkeypatch,
        agent=_review_agent(
            review_trigger=GitHubTriggerConfig(actions=["submitted"]),
        ),
        payload=_review_comment_payload(),
    )

    assert result["fired_agents"] == []
    assert result["skipped"] == [{"agent": "reviewer", "reason": "redundant_review_comment"}]
    assert bus.inbound_queue.empty()
