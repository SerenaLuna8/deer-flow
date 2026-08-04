"""Fail-closed authority boundary for database-managed channel instances."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.channels.instance_identity import persisted_channel_instance_id

ChannelInstanceAuthority = Callable[[str, str], Awaitable[bool]]


class ChannelInstanceAuthorityLost(RuntimeError):
    """The exact project channel instance no longer owns runtime authority."""

    def __init__(self) -> None:
        super().__init__("Channel instance authority is unavailable")


class ChannelInstanceAuthorityGuard:
    """Revalidate exact project instances while bypassing the legacy path."""

    def __init__(
        self,
        authority: ChannelInstanceAuthority | None = None,
    ) -> None:
        self._authority = authority

    def set_authority(self, authority: ChannelInstanceAuthority) -> None:
        self._authority = authority

    async def allows(
        self,
        provider: str,
        channel_instance_id: str,
    ) -> bool:
        try:
            persisted_id = persisted_channel_instance_id(
                provider,
                channel_instance_id,
            )
            if persisted_id is None:
                return True
            if self._authority is None:
                return False
            return bool(await self._authority(provider, persisted_id))
        except Exception:
            return False

    async def require(
        self,
        provider: str,
        channel_instance_id: str,
    ) -> None:
        if not await self.allows(provider, channel_instance_id):
            raise ChannelInstanceAuthorityLost()


__all__ = [
    "ChannelInstanceAuthority",
    "ChannelInstanceAuthorityGuard",
    "ChannelInstanceAuthorityLost",
]
