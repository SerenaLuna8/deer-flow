from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.projects.models import ProjectRole


@dataclass(frozen=True)
class InvitationNotificationView:
    id: uuid.UUID
    project_id: uuid.UUID
    project_slug: str
    project_display_name: str
    inviter_email: str
    role: ProjectRole
    status: str
    is_read: bool
    created_at: datetime
    expires_at: datetime
    version: int


@dataclass(frozen=True)
class NotificationPage:
    items: tuple[InvitationNotificationView, ...]
    unread_count: int
    next_cursor: str | None = None


@dataclass(frozen=True)
class NotificationCursor:
    created_at: datetime
    notification_id: uuid.UUID


def encode_notification_cursor(
    created_at: datetime,
    notification_id: uuid.UUID,
) -> str:
    normalized = created_at.astimezone(UTC)
    payload = json.dumps(
        [
            normalized.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            str(notification_id),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_notification_cursor(value: str) -> NotificationCursor:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list) or len(payload) != 2 or not all(isinstance(item, str) for item in payload):
            raise ValueError
        created_at = datetime.fromisoformat(payload[0].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError
        cursor = NotificationCursor(
            created_at=created_at.astimezone(UTC),
            notification_id=uuid.UUID(payload[1]),
        )
        if (
            encode_notification_cursor(
                cursor.created_at,
                cursor.notification_id,
            )
            != value
        ):
            raise ValueError
        return cursor
    except (
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        ValueError,
    ):
        raise ValueError("invalid notification cursor") from None
