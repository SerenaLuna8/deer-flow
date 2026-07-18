"""M7 Task 5 explicit project authority contracts for channels and input polish."""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.channels import manager
from app.gateway.app import app
from app.private_work.connection_inbound import ConnectionInboundResolver
from app.private_work.errors import PrivateWorkNotFound


def test_global_channel_console_and_input_polish_routes_are_gone() -> None:
    paths = {route.path for route in app.routes}

    for path in (
        "/api/channels",
        "/api/channels/providers",
        "/api/console/stats",
        "/api/input-polish",
    ):
        assert path not in paths
    assert "/api/projects/{project_id}/private-work/input-polish" in paths


def test_inbound_connection_authority_requires_exact_account_coordinate() -> None:
    connection = {
        "id": "connection-a",
        "project_id": str(uuid.uuid4()),
        "owner_user_id": str(uuid.uuid4()),
        "status": "connected",
    }

    with pytest.raises(PrivateWorkNotFound):
        ConnectionInboundResolver._connection_coordinates(connection, "m7-authority")


def test_channel_manager_has_no_auth_disabled_or_external_user_identity_fallback() -> None:
    source = inspect.getsource(manager)

    assert "_auth_disabled_owner_user_id" not in source
    assert "return _safe_user_id_for_run(msg.user_id)" not in source
