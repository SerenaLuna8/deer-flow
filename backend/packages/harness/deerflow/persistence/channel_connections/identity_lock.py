"""PostgreSQL transaction locks for globally unique channel identities."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ChannelIdentity = tuple[str, str, str]


def channel_identity_lock_key(identity: ChannelIdentity) -> int:
    """Return a deterministic signed int64 key with unambiguous field encoding."""

    encoded = bytearray()
    for value in identity:
        raw = value.encode("utf-8")
        encoded.extend(len(raw).to_bytes(4, "big"))
        encoded.extend(raw)
    digest = hashlib.sha256(encoded).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


async def lock_channel_identities(
    session: AsyncSession,
    identities: Iterable[ChannelIdentity],
) -> None:
    """Acquire every identity lock once, in global numeric order."""

    keys = sorted({channel_identity_lock_key(identity) for identity in identities})
    for key in keys:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:identity_key)"),
            {"identity_key": key},
        )
