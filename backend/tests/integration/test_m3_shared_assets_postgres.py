from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import StrEnum

import pytest
from support.m3_shared_assets import M3Scenario

from app.shared_assets.errors import (
    AssetForbidden,
    AssetNotFound,
    AssetResolutionUnavailable,
)


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_value(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _assert_sentinel_absent(value: object, sentinel: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            assert sentinel not in str(key)
            _assert_sentinel_absent(item, sentinel)
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            _assert_sentinel_absent(item, sentinel)
        return
    assert sentinel not in str(value)


@pytest.mark.asyncio
async def test_m3_end_to_end_shared_asset_governance(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    try:
        published = await scenario.publish_system_catalog()
        binding = await scenario.bind_system_agent(published.agent_v1)
        await scenario.publish_system_agent_v2()

        assert binding.version_id == published.agent_v1
        assert (await scenario.resolve_bound_agent()).version_id == published.agent_v1

        with pytest.raises(AssetForbidden):
            await scenario.editor_approve_project_mcp()
        with pytest.raises(AssetNotFound):
            await scenario.other_project_read_project_agent()

        await scenario.suspend_bound_system_agent()
        with pytest.raises(AssetResolutionUnavailable):
            await scenario.resolve_bound_agent()

        snapshot = await scenario.resolve_project_mcp_before_revoke()
        assert snapshot.credential_grant_ids
        secret_sentinel = scenario.credential_secret_sentinel()
        serialized_snapshot = _json_value(snapshot)
        _assert_sentinel_absent(serialized_snapshot, secret_sentinel)
        assert secret_sentinel not in json.dumps(
            serialized_snapshot,
            ensure_ascii=False,
            sort_keys=True,
        )

        await scenario.revoke_project_credential()
        with pytest.raises(AssetResolutionUnavailable):
            await scenario.resolve_project_mcp()
    finally:
        await scenario.close()
