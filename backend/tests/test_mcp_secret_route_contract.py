import uuid

from app.gateway.routers.project_assets import _mcp_secret_response as _legacy_mcp_secret_response
from app.shared_assets.mcp_secret_service import (
    McpSecretSetView,
    McpSecretSlotStatus,
)


def test_mcp_secret_response_legacy_alias_is_exact() -> None:
    from app.gateway.routers.project_asset_routes.mcp import _mcp_secret_response

    assert _legacy_mcp_secret_response is _mcp_secret_response


def test_mcp_secret_response_projects_only_safe_slot_status() -> None:
    from app.gateway.routers.project_asset_routes.mcp import _mcp_secret_response

    mcp_server_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    version_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    slot_id = uuid.UUID("33333333-3333-4333-8333-333333333333")

    response = _mcp_secret_response(
        McpSecretSetView(
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=version_id,
            revision=4,
            readiness="ready",
            slots=(
                McpSecretSlotStatus(
                    id=slot_id,
                    name="auth",
                    purpose="Request authentication",
                    payload_schema={"headers": ("Authorization",)},
                    required=True,
                    configured=True,
                    revision=4,
                ),
            ),
        ),
        "request-1",
    )

    assert response.model_dump(mode="json") == {
        "mcp_server_id": str(mcp_server_id),
        "mcp_server_version_id": str(version_id),
        "revision": 4,
        "readiness": "ready",
        "slots": [
            {
                "id": str(slot_id),
                "name": "auth",
                "purpose": "Request authentication",
                "payload_schema": {"headers": ["Authorization"]},
                "required": True,
                "configured": True,
                "revision": 4,
            }
        ],
        "request_id": "request-1",
    }
