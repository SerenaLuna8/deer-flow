from __future__ import annotations

import re
import uuid
from typing import Protocol

from app.reliability.owner_refs import AuditHmacKeyring

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class ChannelGroupIdentityHasher(Protocol):
    def group_ref(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_chat_id: str,
    ) -> str: ...

    def account_ref(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_account_id: str,
    ) -> str: ...

    def group_refs(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_chat_id: str,
    ) -> tuple[str, ...]: ...

    def account_refs(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_account_id: str,
    ) -> tuple[str, ...]: ...

    def topic_ref(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_topic_id: str,
    ) -> str: ...

    def topic_refs(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_topic_id: str,
    ) -> tuple[str, ...]: ...


class AuditChannelGroupIdentityHasher:
    """Domain-separated irreversible references for provider identifiers."""

    def __init__(self, keyring: AuditHmacKeyring | None = None) -> None:
        self._keyring = keyring

    @classmethod
    def from_environment(cls) -> AuditChannelGroupIdentityHasher:
        return cls(AuditHmacKeyring.from_environment())

    @staticmethod
    def _validate(
        provider: str,
        instance_id: uuid.UUID,
        external_id: str,
    ) -> tuple[str, uuid.UUID, str]:
        if type(provider) is not str or _PROVIDER.fullmatch(provider) is None:
            raise ValueError("channel provider is invalid")
        if type(instance_id) is not uuid.UUID:
            raise ValueError("channel instance is invalid")
        if type(external_id) is not str:
            raise ValueError("external channel identity is invalid")
        normalized = external_id.strip()
        if not normalized or normalized != external_id or len(normalized.encode("utf-8")) > 512:
            raise ValueError("external channel identity is invalid")
        return provider, instance_id, normalized

    def group_ref(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_chat_id: str,
    ) -> str:
        provider, instance_id, external_chat_id = self._validate(
            provider,
            instance_id,
            external_chat_id,
        )
        keyring = self._keyring or AuditHmacKeyring.from_environment()
        return keyring.channel_external_ref(
            "group",
            provider,
            instance_id,
            external_chat_id,
        )

    def group_refs(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_chat_id: str,
    ) -> tuple[str, ...]:
        provider, instance_id, external_chat_id = self._validate(
            provider,
            instance_id,
            external_chat_id,
        )
        keyring = self._keyring or AuditHmacKeyring.from_environment()
        return keyring.channel_external_refs(
            "group",
            provider,
            instance_id,
            external_chat_id,
        )

    def account_ref(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_account_id: str,
    ) -> str:
        provider, instance_id, external_account_id = self._validate(
            provider,
            instance_id,
            external_account_id,
        )
        keyring = self._keyring or AuditHmacKeyring.from_environment()
        return keyring.channel_external_ref(
            "account",
            provider,
            instance_id,
            external_account_id,
        )

    def account_refs(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_account_id: str,
    ) -> tuple[str, ...]:
        provider, instance_id, external_account_id = self._validate(
            provider,
            instance_id,
            external_account_id,
        )
        keyring = self._keyring or AuditHmacKeyring.from_environment()
        return keyring.channel_external_refs(
            "account",
            provider,
            instance_id,
            external_account_id,
        )

    def topic_ref(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_topic_id: str,
    ) -> str:
        provider, instance_id, external_topic_id = self._validate(
            provider,
            instance_id,
            external_topic_id,
        )
        keyring = self._keyring or AuditHmacKeyring.from_environment()
        return keyring.channel_external_ref(
            "topic",
            provider,
            instance_id,
            external_topic_id,
        )

    def topic_refs(
        self,
        provider: str,
        instance_id: uuid.UUID,
        external_topic_id: str,
    ) -> tuple[str, ...]:
        provider, instance_id, external_topic_id = self._validate(
            provider,
            instance_id,
            external_topic_id,
        )
        keyring = self._keyring or AuditHmacKeyring.from_environment()
        return keyring.channel_external_refs(
            "topic",
            provider,
            instance_id,
            external_topic_id,
        )


__all__ = ["AuditChannelGroupIdentityHasher", "ChannelGroupIdentityHasher"]
