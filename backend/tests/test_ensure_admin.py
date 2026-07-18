"""Tests for the marker-free first-boot admin status check."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_first_boot_does_not_create_admin() -> None:
    """The Gateway reports first boot without mutating auth or private data."""
    provider = AsyncMock()
    provider.count_admin_users.return_value = 0

    with patch("app.gateway.deps.get_local_provider", return_value=provider):
        from app.gateway.app import _ensure_admin_user

        await _ensure_admin_user()

    provider.count_admin_users.assert_awaited_once_with()
    provider.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_admin_requires_no_legacy_private_migration() -> None:
    """An existing admin only completes the status probe after M7 cleanup."""
    provider = AsyncMock()
    provider.count_admin_users.return_value = 1

    with patch("app.gateway.deps.get_local_provider", return_value=provider):
        from app.gateway.app import _ensure_admin_user

        await _ensure_admin_user()

    provider.count_admin_users.assert_awaited_once_with()
