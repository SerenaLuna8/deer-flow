from app.gateway.routers.project_assets import (
    _credential_pending_migration_response,
)
from app.shared_assets.credential_service import (
    CredentialMigrationReferenceView,
    CredentialPendingMigrationView,
)


def test_migration_status_serializes_stale_and_current_consumer_details() -> None:
    response = _credential_pending_migration_response(
        CredentialPendingMigrationView(
            total=1,
            mcp_grant_count=0,
            skill_binding_count=1,
            system_model_count=0,
            references=(
                CredentialMigrationReferenceView(
                    kind="skill_binding",
                    display_name="Old Skill",
                    version_number=1,
                    reference_name="TARGET_KEY",
                    source_name="SOURCE_KEY",
                ),
            ),
            current_reference_count=1,
            current_references=(
                CredentialMigrationReferenceView(
                    kind="mcp_grant",
                    display_name="Current MCP",
                    version_number=2,
                    reference_name="auth-slot",
                ),
            ),
        )
    )

    assert response.references[0].display_name == "Old Skill"
    assert response.references[0].source_name == "SOURCE_KEY"
    assert response.current_references[0].display_name == "Current MCP"
    assert response.current_references[0].source_name is None
