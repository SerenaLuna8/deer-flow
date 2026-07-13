"""Add typed system and project shared asset schema.

Revision ID: 0007_project_shared_assets
Revises: 0006_project_governance
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_project_shared_assets"
down_revision: str | Sequence[str] | None = "0006_project_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


M3_TABLES: tuple[str, ...] = (
    "agents",
    "agent_versions",
    "agent_version_skill_refs",
    "agent_version_mcp_refs",
    "skills",
    "skill_versions",
    "skill_version_files",
    "mcp_servers",
    "mcp_server_versions",
    "mcp_version_credential_slots",
    "credentials",
    "credential_versions",
    "credential_envelopes",
    "credential_grants",
    "project_system_agent_bindings",
    "project_system_skill_bindings",
    "project_system_mcp_bindings",
    "asset_catalog_state",
)


def _create_asset_table(table: str, *, current_version_column: str) -> None:
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column(current_version_column, sa.Uuid(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_key", sa.String(255), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)",
            name=f"ck_{table}_scope_project",
        ),
        sa.CheckConstraint("status IN ('active', 'archived', 'suspended')", name=f"ck_{table}_status"),
        sa.CheckConstraint("version >= 1", name=f"ck_{table}_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "scope", name=f"uq_{table}_id_scope"),
        sa.UniqueConstraint("source_key", name=f"uq_{table}_source_key"),
    )
    op.create_index(
        f"uq_{table}_system_slug",
        table,
        [sa.text("lower(slug)")],
        unique=True,
        postgresql_where=sa.text("scope = 'system'"),
    )
    op.create_index(
        f"uq_{table}_project_slug",
        table,
        ["project_id", sa.text("lower(slug)")],
        unique=True,
        postgresql_where=sa.text("scope = 'project'"),
    )


def _workflow_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(36), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def _create_credentials() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("credential_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_key", sa.String(255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(36), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)",
            name="ck_credentials_scope_project",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_credentials_status"),
        sa.CheckConstraint("version >= 1", name="ck_credentials_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "scope", name="uq_credentials_id_scope"),
        sa.UniqueConstraint("source_key", name="uq_credentials_source_key"),
    )
    op.create_index(
        "uq_credentials_system_name",
        "credentials",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'system'"),
    )
    op.create_index(
        "uq_credentials_project_name",
        "credentials",
        ["project_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'project'"),
    )


def _create_versions() -> None:
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("workflow_status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("soul", sa.Text(), nullable=False),
        sa.Column("model_ref", sa.String(255), nullable=False),
        sa.Column("tool_groups", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("payload_checksum", sa.CHAR(64), nullable=False),
        *_workflow_columns(),
        sa.CheckConstraint("version_number >= 1", name="ck_agent_versions_number"),
        sa.CheckConstraint(
            "workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')",
            name="ck_agent_versions_workflow_status",
        ),
        sa.CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_agent_versions_checksum"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_asset_number"),
        sa.UniqueConstraint("agent_id", "id", name="uq_agent_versions_asset_id"),
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("workflow_status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("frontmatter", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("compatibility", sa.String(255), nullable=True),
        sa.Column("secret_requirements", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("scan_decision", sa.String(24), nullable=False),
        sa.Column("scan_summary", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("payload_checksum", sa.CHAR(64), nullable=False),
        *_workflow_columns(),
        sa.CheckConstraint("version_number >= 1", name="ck_skill_versions_number"),
        sa.CheckConstraint(
            "workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')",
            name="ck_skill_versions_workflow_status",
        ),
        sa.CheckConstraint("scan_decision IN ('allow', 'warn', 'block')", name="ck_skill_versions_scan_decision"),
        sa.CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_skill_versions_checksum"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_asset_number"),
        sa.UniqueConstraint("skill_id", "id", name="uq_skill_versions_asset_id"),
    )
    op.create_table(
        "mcp_server_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("workflow_status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("transport", sa.String(24), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("args", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("non_secret_env", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("non_secret_headers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("oauth_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("routing", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("tool_overrides", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("payload_checksum", sa.CHAR(64), nullable=False),
        *_workflow_columns(),
        sa.CheckConstraint("version_number >= 1", name="ck_mcp_server_versions_number"),
        sa.CheckConstraint(
            "workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')",
            name="ck_mcp_server_versions_workflow_status",
        ),
        sa.CheckConstraint(
            "transport IN ('stdio', 'sse', 'http', 'streamable_http')",
            name="ck_mcp_server_versions_transport",
        ),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_mcp_server_versions_timeout"),
        sa.CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_mcp_server_versions_checksum"),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mcp_server_id", "version_number", name="uq_mcp_server_versions_asset_number"),
        sa.UniqueConstraint("mcp_server_id", "id", name="uq_mcp_server_versions_asset_id"),
    )
    op.create_table(
        "credential_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("payload_schema", postgresql.JSONB(), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(36), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("version_number >= 1", name="ck_credential_versions_number"),
        sa.CheckConstraint("status IN ('active', 'retired', 'revoked')", name="ck_credential_versions_status"),
        sa.CheckConstraint("payload_schema_version >= 1", name="ck_credential_versions_payload_schema_version"),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["credential_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id", "version_number", name="uq_credential_versions_asset_number"),
        sa.UniqueConstraint("credential_id", "id", name="uq_credential_versions_asset_id"),
    )


def _add_current_version_foreign_keys() -> None:
    op.create_foreign_key(
        "fk_agents_current_published_version",
        "agents",
        "agent_versions",
        ["id", "current_published_version_id"],
        ["agent_id", "id"],
    )
    op.create_foreign_key(
        "fk_skills_current_published_version",
        "skills",
        "skill_versions",
        ["id", "current_published_version_id"],
        ["skill_id", "id"],
    )
    op.create_foreign_key(
        "fk_mcp_servers_current_published_version",
        "mcp_servers",
        "mcp_server_versions",
        ["id", "current_published_version_id"],
        ["mcp_server_id", "id"],
    )
    op.create_foreign_key(
        "fk_credentials_current_version",
        "credentials",
        "credential_versions",
        ["id", "current_version_id"],
        ["credential_id", "id"],
    )


def _create_version_payload_tables() -> None:
    op.create_table(
        "skill_version_files",
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))'", name="ck_skill_version_files_safe_path"),
        sa.CheckConstraint("size_bytes >= 0 AND size_bytes <= 104857600", name="ck_skill_version_files_size"),
        sa.CheckConstraint("size_bytes = octet_length(content)", name="ck_skill_version_files_content_size"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_skill_version_files_sha256"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("skill_version_id", "path"),
    )
    op.create_table(
        "mcp_version_credential_slots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_version_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("purpose", sa.Text(), server_default="", nullable=False),
        sa.Column("payload_schema", postgresql.JSONB(), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["mcp_server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mcp_server_version_id", "name", name="uq_mcp_credential_slots_version_name"),
        sa.UniqueConstraint("mcp_server_version_id", "id", name="uq_mcp_credential_slots_version_id"),
    )
    op.create_table(
        "agent_version_skill_refs",
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("sort_order >= 0", name="ck_agent_version_skill_refs_sort_order"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("agent_version_id", "skill_version_id"),
    )
    op.create_table(
        "agent_version_mcp_refs",
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_version_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("sort_order >= 0", name="ck_agent_version_mcp_refs_sort_order"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mcp_server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("agent_version_id", "mcp_server_version_id"),
    )


def _create_credential_tables() -> None:
    op.create_table(
        "credential_envelopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column("envelope_generation", sa.BigInteger(), nullable=False),
        sa.Column("key_id", sa.String(255), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("rotated_from_envelope_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("envelope_generation >= 1", name="ck_credential_envelopes_generation"),
        sa.CheckConstraint("octet_length(nonce) = 12", name="ck_credential_envelopes_nonce_size"),
        sa.CheckConstraint("octet_length(ciphertext) >= 16", name="ck_credential_envelopes_ciphertext_size"),
        sa.ForeignKeyConstraint(["credential_version_id"], ["credential_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["rotated_from_envelope_id"], ["credential_envelopes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_version_id", "envelope_generation", name="uq_credential_envelopes_version_generation"),
    )
    op.create_index(
        "uq_credential_envelopes_active_version",
        "credential_envelopes",
        ["credential_version_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "credential_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_version_id", sa.Uuid(), nullable=False),
        sa.Column("credential_slot_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(36), nullable=True),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_credential_grants_status"),
        sa.CheckConstraint("version >= 1", name="ck_credential_grants_version"),
        sa.ForeignKeyConstraint(
            ["mcp_server_version_id", "credential_slot_id"],
            ["mcp_version_credential_slots.mcp_server_version_id", "mcp_version_credential_slots.id"],
            name="fk_credential_grants_slot_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["credential_version_id"], ["credential_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_credential_grants_active_slot",
        "credential_grants",
        ["mcp_server_version_id", "credential_slot_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def _create_binding_table(
    table: str,
    *,
    asset_table: str,
    asset_id_column: str,
    version_table: str,
    version_asset_column: str,
    version_id_column: str,
) -> None:
    op.create_table(
        table,
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(asset_id_column, sa.Uuid(), nullable=False),
        sa.Column("system_asset_scope", sa.String(16), server_default="system", nullable=False),
        sa.Column(version_id_column, sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("updated_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("system_asset_scope = 'system'", name=f"ck_{table}_system_scope"),
        sa.CheckConstraint("version >= 1", name=f"ck_{table}_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            [asset_id_column, "system_asset_scope"],
            [f"{asset_table}.id", f"{asset_table}.scope"],
            name=f"fk_{table}_system_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [asset_id_column, version_id_column],
            [f"{version_table}.{version_asset_column}", f"{version_table}.id"],
            name=f"fk_{table}_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("project_id", asset_id_column),
    )


def _create_bindings_and_state() -> None:
    _create_binding_table(
        "project_system_agent_bindings",
        asset_table="agents",
        asset_id_column="system_agent_id",
        version_table="agent_versions",
        version_asset_column="agent_id",
        version_id_column="agent_version_id",
    )
    _create_binding_table(
        "project_system_skill_bindings",
        asset_table="skills",
        asset_id_column="system_skill_id",
        version_table="skill_versions",
        version_asset_column="skill_id",
        version_id_column="skill_version_id",
    )
    _create_binding_table(
        "project_system_mcp_bindings",
        asset_table="mcp_servers",
        asset_id_column="system_mcp_server_id",
        version_table="mcp_server_versions",
        version_asset_column="mcp_server_id",
        version_id_column="mcp_server_version_id",
    )
    op.create_table(
        "asset_catalog_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_asset_catalog_state_singleton"),
        sa.CheckConstraint("generation >= 1", name="ck_asset_catalog_state_generation"),
        sa.PrimaryKeyConstraint("id"),
    )


_CREATE_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id'
    ]::text[]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id'
    ]::text[]) THEN
        RAISE EXCEPTION 'shared asset version payload is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_GENERATION_FUNCTION = """
CREATE OR REPLACE FUNCTION bump_asset_catalog_generation()
RETURNS trigger AS $$
BEGIN
    INSERT INTO asset_catalog_state (id, generation, updated_at)
    VALUES (1, 1, now())
    ON CONFLICT (id) DO UPDATE
      SET generation = asset_catalog_state.generation + 1,
          updated_at = now();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_TRIGGER_DDL = (
    _CREATE_IMMUTABLE_FUNCTION,
    _CREATE_GENERATION_FUNCTION,
    "CREATE TRIGGER trg_agent_versions_immutable BEFORE UPDATE ON agent_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_versions_immutable BEFORE UPDATE ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_credential_versions_immutable BEFORE UPDATE ON credential_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_skill_refs_immutable BEFORE UPDATE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_mcp_refs_immutable BEFORE UPDATE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_credential_slots_immutable BEFORE UPDATE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agents_generation AFTER UPDATE OF status, current_published_version_id ON agents FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skills_generation AFTER UPDATE OF status, current_published_version_id ON skills FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_servers_generation AFTER UPDATE OF status, current_published_version_id ON mcp_servers FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_agent_versions_generation AFTER UPDATE OF workflow_status ON agent_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_versions_generation AFTER UPDATE OF workflow_status ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_server_versions_generation AFTER UPDATE OF workflow_status ON mcp_server_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credentials_generation AFTER UPDATE OF status, current_version_id ON credentials FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credential_versions_generation AFTER UPDATE OF status ON credential_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_agent_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_agent_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_skill_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_mcp_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credential_grants_generation AFTER INSERT OR UPDATE OR DELETE ON credential_grants FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
)


def upgrade() -> None:
    _create_asset_table("agents", current_version_column="current_published_version_id")
    _create_asset_table("skills", current_version_column="current_published_version_id")
    _create_asset_table("mcp_servers", current_version_column="current_published_version_id")
    _create_credentials()
    _create_versions()
    _add_current_version_foreign_keys()
    _create_version_payload_tables()
    _create_credential_tables()
    _create_bindings_and_state()
    for statement in _TRIGGER_DDL:
        op.execute(sa.text(statement))


def _m3_has_data() -> bool:
    clauses = " OR ".join(f"EXISTS (SELECT 1 FROM {table} LIMIT 1)" for table in M3_TABLES)
    return bool(op.get_bind().execute(sa.text(f"SELECT {clauses}")).scalar_one())


def downgrade() -> None:
    if _m3_has_data():
        raise RuntimeError("M3 shared asset data exists; refusing destructive downgrade")

    op.drop_constraint("fk_credentials_current_version", "credentials", type_="foreignkey")
    op.drop_constraint("fk_mcp_servers_current_published_version", "mcp_servers", type_="foreignkey")
    op.drop_constraint("fk_skills_current_published_version", "skills", type_="foreignkey")
    op.drop_constraint("fk_agents_current_published_version", "agents", type_="foreignkey")

    op.drop_table("project_system_mcp_bindings")
    op.drop_table("project_system_skill_bindings")
    op.drop_table("project_system_agent_bindings")
    op.drop_index("uq_credential_grants_active_slot", table_name="credential_grants")
    op.drop_table("credential_grants")
    op.drop_index("uq_credential_envelopes_active_version", table_name="credential_envelopes")
    op.drop_table("credential_envelopes")
    op.drop_table("agent_version_mcp_refs")
    op.drop_table("agent_version_skill_refs")
    op.drop_table("mcp_version_credential_slots")
    op.drop_table("skill_version_files")
    op.drop_table("credential_versions")
    op.drop_table("mcp_server_versions")
    op.drop_table("skill_versions")
    op.drop_table("agent_versions")
    op.drop_index("uq_credentials_project_name", table_name="credentials")
    op.drop_index("uq_credentials_system_name", table_name="credentials")
    op.drop_table("credentials")
    op.drop_index("uq_mcp_servers_project_slug", table_name="mcp_servers")
    op.drop_index("uq_mcp_servers_system_slug", table_name="mcp_servers")
    op.drop_table("mcp_servers")
    op.drop_index("uq_skills_project_slug", table_name="skills")
    op.drop_index("uq_skills_system_slug", table_name="skills")
    op.drop_table("skills")
    op.drop_index("uq_agents_project_slug", table_name="agents")
    op.drop_index("uq_agents_system_slug", table_name="agents")
    op.drop_table("agents")
    op.drop_table("asset_catalog_state")
    op.execute(sa.text("DROP FUNCTION IF EXISTS bump_asset_catalog_generation()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_shared_asset_version_payload_update()"))
