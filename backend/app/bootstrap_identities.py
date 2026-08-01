"""Canonical non-login identities used only by explicit database bootstrap."""

from __future__ import annotations

import uuid

BUILTIN_ASSET_USER_ID = uuid.UUID(
    "00000000-0000-0000-0000-000000000007",
)
BUILTIN_ASSET_EMAIL = "builtin-assets@deerflow.invalid"

BUILTIN_MODEL_USER_ID = uuid.UUID(
    "00000000-0000-0000-0000-000000000008",
)
BUILTIN_MODEL_EMAIL = "builtin-models@deerflow.invalid"

BUILTIN_SERVICE_USER_IDS = frozenset(
    {
        BUILTIN_ASSET_USER_ID,
        BUILTIN_MODEL_USER_ID,
    }
)

__all__ = [
    "BUILTIN_ASSET_EMAIL",
    "BUILTIN_ASSET_USER_ID",
    "BUILTIN_MODEL_EMAIL",
    "BUILTIN_MODEL_USER_ID",
    "BUILTIN_SERVICE_USER_IDS",
]
