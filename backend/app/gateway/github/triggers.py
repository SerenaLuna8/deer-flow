"""Pure GitHub event filters for trigger configurations supplied by callers.

The registry currently returns no bindings until project-scoped lookup exists.
"""

from __future__ import annotations

import re
from typing import Any

from deerflow.config.agents_config import GitHubTriggerConfig


def _action(payload: dict[str, Any]) -> str | None:
    action = payload.get("action")
    return action if isinstance(action, str) else None


def _comment_body(event: str, payload: dict[str, Any]) -> str:
    """Extract the human-typed text to scan for an ``@mention``.

    For comment events this is the comment body. For ``issues`` and
    ``pull_request`` events there is no separate comment — the mention
    would be in the issue/PR body itself — so we read that. For
    ``pull_request_review`` the body is the review summary. Other events
    have no user-authored text to mention-check and return ``""``.
    """
    if event in ("issue_comment", "pull_request_review_comment"):
        body = (payload.get("comment") or {}).get("body")
        return body if isinstance(body, str) else ""
    if event == "issues":
        body = (payload.get("issue") or {}).get("body")
        return body if isinstance(body, str) else ""
    if event == "pull_request":
        body = (payload.get("pull_request") or {}).get("body")
        return body if isinstance(body, str) else ""
    if event == "pull_request_review":
        body = (payload.get("review") or {}).get("body")
        return body if isinstance(body, str) else ""
    return ""


def _author_login(event: str, payload: dict[str, Any]) -> str | None:
    """Login of the human who triggered the event, for ``allow_authors``."""
    if event in ("issue_comment", "pull_request_review_comment"):
        login = (payload.get("comment") or {}).get("user", {}).get("login")
    elif event == "pull_request":
        login = (payload.get("pull_request") or {}).get("user", {}).get("login")
    elif event == "pull_request_review":
        login = (payload.get("review") or {}).get("user", {}).get("login")
    elif event == "issues":
        login = (payload.get("issue") or {}).get("user", {}).get("login")
    else:
        login = (payload.get("sender") or {}).get("login")
    return login if isinstance(login, str) else None


def _mentions(body: str, login: str) -> bool:
    """Return True if ``body`` @-mentions ``login`` with proper boundaries.

    GitHub logins are ``[A-Za-z0-9-]+``, so the character immediately
    after the login in a mention must NOT be one of those — otherwise
    ``@deerflow`` would falsely match ``@deerflow-bot`` (a different,
    legitimate GitHub user). A plain substring ``in`` check is wrong for
    this reason.

    Also rejects mentions where the ``@`` is preceded by a login-class
    character (e.g. ``foo@deerflow`` inside an email address) to avoid
    incidental matches on URLs / pasted addresses.

    Match is case-insensitive; GitHub itself is.
    """
    pattern = rf"(?:^|[^A-Za-z0-9-])@{re.escape(login)}(?![A-Za-z0-9-])"
    return re.search(pattern, body, flags=re.IGNORECASE) is not None


def event_should_fire(
    event: str,
    payload: dict[str, Any],
    trigger: GitHubTriggerConfig,
    default_mention_login: str,
) -> tuple[bool, str]:
    """Decide whether ``event`` fires the agent for this binding.

    Args:
        event: GitHub event name (``X-GitHub-Event``).
        payload: Parsed webhook payload.
        trigger: Trigger configuration whose declared gates are applied as-is.
        default_mention_login: Bot login (without ``@``) used by
            ``require_mention`` when the trigger doesn't override
            ``mention_login``. Pass the agent name as a fallback.

    Returns:
        ``(fire, reason)`` where ``fire`` is the decision and ``reason``
        is a short label for logging (e.g. ``"action=opened"``,
        ``"mention"``, ``"disabled"``).
    """
    # Action whitelist (e.g. only "opened" PRs).
    if trigger.actions is not None:
        action = _action(payload)
        if action not in trigger.actions:
            return False, f"action={action!r} not in {trigger.actions}"

    # allow_authors bypasses require_mention entirely. Useful so a repo
    # owner can talk to the bot without typing the handle every time.
    if trigger.allow_authors:
        author = _author_login(event, payload)
        if author and author.lower() in {allowed_author.lower() for allowed_author in trigger.allow_authors}:
            return True, f"allow_authors:{author}"

    if trigger.require_mention:
        login = trigger.mention_login or default_mention_login
        body = _comment_body(event, payload)
        # Boundary-aware @-mention match: ``@deerflow`` must NOT match
        # ``@deerflow-bot`` (a distinct, legitimate GitHub login). See
        # :func:`_mentions` for the full rationale.
        if not login or not _mentions(body, login):
            return False, f"mention required for @{login}"

    # All gates passed.
    action = _action(payload)
    return True, f"action={action}" if action else "ok"
