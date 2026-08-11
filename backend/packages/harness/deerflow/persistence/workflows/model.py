"""Final-schema ORM rows for standalone, owner-private Workflow execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


_EMPTY_PROFILE_KEY = "0" * 64


class WorkflowDefinitionRow(Base):
    __tablename__ = "workflow_definitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))
    current_published_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_workflow_definitions_status"),
        CheckConstraint("revision >= 1", name="ck_workflow_definitions_revision"),
        CheckConstraint("name = btrim(name) AND char_length(name) BETWEEN 1 AND 255", name="ck_workflow_definitions_name"),
        UniqueConstraint("id", "project_id", name="uq_workflow_definitions_id_project"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_workflow_definitions_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_workflow_definitions_created_by", ondelete="RESTRICT"),
        ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_workflow_definitions_updated_by", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["id", "current_published_version_id"],
            ["workflow_versions.workflow_id", "workflow_versions.id"],
            name="fk_workflow_definitions_current_version",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("uq_workflow_definitions_project_name", "project_id", func.lower(name), unique=True),
        Index("ix_workflow_definitions_list", "project_id", "status", updated_at.desc(), id.desc()),
    )


class WorkflowDraftRow(Base):
    __tablename__ = "workflow_drafts"

    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    spec_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    canvas_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    spec_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    canvas_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    draft_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_workflow_drafts_revision"),
        CheckConstraint("spec_schema_version >= 1 AND canvas_schema_version >= 1", name="ck_workflow_drafts_schema"),
        CheckConstraint("jsonb_typeof(spec_json) = 'object'", name="ck_workflow_drafts_spec_object"),
        CheckConstraint("jsonb_typeof(canvas_json) = 'object'", name="ck_workflow_drafts_canvas_object"),
        CheckConstraint("draft_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_drafts_checksum"),
        ForeignKeyConstraint(
            ["workflow_id", "project_id"],
            ["workflow_definitions.id", "workflow_definitions.project_id"],
            name="fk_workflow_drafts_definition",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_workflow_drafts_updated_by", ondelete="RESTRICT"),
    )


class WorkflowVersionRow(Base):
    __tablename__ = "workflow_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    graph_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default=text("1"))
    canvas_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default=text("1"))
    compiler_contract_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    spec_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    canvas_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    semantic_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    published_by: Mapped[str] = mapped_column(String(36), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_workflow_versions_number"),
        CheckConstraint(
            "graph_schema_version >= 1 AND canvas_schema_version >= 1 AND compiler_contract_version >= 1",
            name="ck_workflow_versions_schema",
        ),
        CheckConstraint("jsonb_typeof(spec_json) = 'object'", name="ck_workflow_versions_spec_object"),
        CheckConstraint("jsonb_typeof(canvas_json) = 'object'", name="ck_workflow_versions_canvas_object"),
        CheckConstraint("semantic_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_versions_checksum"),
        UniqueConstraint("workflow_id", "version_number", name="uq_workflow_versions_number"),
        UniqueConstraint("workflow_id", "id", name="uq_workflow_versions_workflow_id"),
        UniqueConstraint("id", "project_id", name="uq_workflow_versions_id_project"),
        UniqueConstraint("id", "workflow_id", "project_id", name="uq_workflow_versions_scope"),
        UniqueConstraint(
            "id",
            "project_id",
            "graph_schema_version",
            "compiler_contract_version",
            "semantic_checksum",
            name="uq_workflow_versions_snapshot_exact",
        ),
        UniqueConstraint(
            "workflow_id",
            "semantic_checksum",
            "compiler_contract_version",
            name="uq_workflow_versions_semantic_contract",
        ),
        ForeignKeyConstraint(
            ["workflow_id", "project_id"],
            ["workflow_definitions.id", "workflow_definitions.project_id"],
            name="fk_workflow_versions_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["published_by"], ["users.id"], name="fk_workflow_versions_published_by", ondelete="RESTRICT"),
    )


class WorkflowControlOperationRow(Base):
    """Durable, content-free idempotency receipt for control-plane writes."""

    __tablename__ = "workflow_control_operations"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    result_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    # v12 extends the frozen v11 physical order. Keep these columns after the
    # original receipt fields so fresh installs and v5->v12 ALTERs are catalog
    # identical without rewriting the append-only authority.
    scope_key: Mapped[str] = mapped_column(String(512), nullable=False)
    result_revision: Mapped[int | None] = mapped_column(BigInteger)
    result_checksum: Mapped[str | None] = mapped_column(CHAR(64))
    result_slot_id: Mapped[str | None] = mapped_column(String(128))
    result_credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    result_credential_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    result_status: Mapped[str | None] = mapped_column(String(16))
    result_deleted: Mapped[bool | None] = mapped_column(Boolean)
    result_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_name: Mapped[str | None] = mapped_column(String(255))
    result_description: Mapped[str | None] = mapped_column(String(4096))
    result_lifecycle: Mapped[str | None] = mapped_column(String(16))
    result_published_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    result_published_version_number: Mapped[int | None] = mapped_column(BigInteger)
    result_draft_revision: Mapped[int | None] = mapped_column(BigInteger)
    result_draft_checksum: Mapped[str | None] = mapped_column(CHAR(64))
    result_missing_slot_ids_csv: Mapped[str | None] = mapped_column(String(33000))

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "operation",
            "scope_key",
            "idempotency_hash",
            name="pk_workflow_control_operations",
        ),
        CheckConstraint(
            "operation IN ('create','update','save_draft','archive','publish','draft_grant_put','draft_grant_delete','version_grant_put','version_grant_delete')",
            name="ck_workflow_control_operations_operation",
        ),
        CheckConstraint(
            # PostgreSQL rejects bounded regex repetitions above 255. The
            # VARCHAR(512) bound plus this character check is exactly the
            # logical ``^[a-z][A-Za-z0-9:._-]{0,511}$`` contract.
            "char_length(scope_key) BETWEEN 1 AND 512 AND scope_key ~ '^[a-z][A-Za-z0-9:._-]*$'",
            name="ck_workflow_control_operations_scope_key",
        ),
        CheckConstraint(
            "(operation = 'create' AND scope_key = 'project:' || project_id::text) OR "
            "(operation IN ('update','save_draft','archive','publish') AND scope_key = 'definition:' || workflow_id::text) OR "
            "(operation IN ('draft_grant_put','draft_grant_delete') AND scope_key = 'draft-slot:' || workflow_id::text || ':' || result_slot_id) OR "
            "(operation IN ('version_grant_put','version_grant_delete') AND scope_key = 'version-slot:' || workflow_id::text || ':' || result_version_id::text || ':' || result_slot_id)",
            name="ck_workflow_control_operations_scope_shape",
        ),
        CheckConstraint(
            "idempotency_hash ~ '^[0-9a-f]{64}$'",
            name="ck_workflow_control_operations_idempotency_hash",
        ),
        CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_workflow_control_operations_request_digest",
        ),
        CheckConstraint(
            "result_revision IS NULL OR result_revision >= 1",
            name="ck_workflow_control_operations_result_revision",
        ),
        CheckConstraint(
            "result_checksum IS NULL OR result_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_workflow_control_operations_result_checksum",
        ),
        CheckConstraint(
            "result_draft_checksum IS NULL OR result_draft_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_workflow_control_operations_result_draft_checksum",
        ),
        CheckConstraint(
            "result_slot_id IS NULL OR result_slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'",
            name="ck_workflow_control_operations_result_slot",
        ),
        CheckConstraint(
            "result_status IS NULL OR result_status IN ('active','revoked')",
            name="ck_workflow_control_operations_result_status",
        ),
        CheckConstraint(
            "result_lifecycle IS NULL OR result_lifecycle IN ('active','archived')",
            name="ck_workflow_control_operations_result_lifecycle",
        ),
        CheckConstraint(
            "result_published_version_number IS NULL OR result_published_version_number >= 1",
            name="ck_workflow_control_operations_result_published_version_number",
        ),
        CheckConstraint(
            "result_draft_revision IS NULL OR result_draft_revision >= 1",
            name="ck_workflow_control_operations_result_draft_revision",
        ),
        CheckConstraint(
            "result_missing_slot_ids_csv IS NULL OR result_missing_slot_ids_csv = '' OR result_missing_slot_ids_csv ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}(,[A-Za-z_][A-Za-z0-9_.:-]{0,127})*$'",
            name="ck_workflow_control_operations_result_missing_slots",
        ),
        CheckConstraint(
            "(operation IN ('publish','version_grant_put','version_grant_delete')) = (result_version_id IS NOT NULL)",
            name="ck_workflow_control_operations_version_shape",
        ),
        CheckConstraint(
            "(operation IN ('draft_grant_put','draft_grant_delete','version_grant_put','version_grant_delete')) = (result_slot_id IS NOT NULL)",
            name="ck_workflow_control_operations_slot_shape",
        ),
        CheckConstraint(
            "(operation IN ('draft_grant_put','version_grant_put','version_grant_delete') AND result_credential_id IS NOT NULL AND result_credential_version_id IS NOT NULL AND result_checksum IS NOT NULL) OR "
            "(operation NOT IN ('draft_grant_put','version_grant_put','version_grant_delete') AND result_credential_id IS NULL AND result_credential_version_id IS NULL AND result_checksum IS NULL)",
            name="ck_workflow_control_operations_credential_shape",
        ),
        CheckConstraint(
            "(operation = 'draft_grant_delete' AND result_deleted IS TRUE) OR (operation <> 'draft_grant_delete' AND result_deleted IS NULL)",
            name="ck_workflow_control_operations_delete_shape",
        ),
        CheckConstraint(
            "(operation IN ('create','update','archive') "
            "AND result_name IS NOT NULL AND result_description IS NOT NULL "
            "AND result_lifecycle IS NOT NULL AND result_revision IS NOT NULL "
            "AND result_draft_revision IS NOT NULL "
            "AND result_draft_checksum IS NOT NULL "
            "AND result_created_at IS NOT NULL "
            "AND result_updated_at IS NOT NULL) OR "
            "(operation NOT IN ('create','update','archive') "
            "AND result_name IS NULL AND result_description IS NULL "
            "AND result_lifecycle IS NULL "
            "AND result_published_version_id IS NULL "
            "AND result_published_version_number IS NULL "
            "AND result_draft_revision IS NULL)",
            name="ck_workflow_control_operations_definition_shape",
        ),
        CheckConstraint(
            "(operation = 'archive' AND result_lifecycle = 'archived') OR (operation IN ('create','update') AND result_lifecycle = 'active') OR (operation NOT IN ('create','update','archive') AND result_lifecycle IS NULL)",
            name="ck_workflow_control_operations_lifecycle_shape",
        ),
        CheckConstraint(
            "(result_published_version_id IS NULL AND result_published_version_number IS NULL) OR (result_published_version_id IS NOT NULL AND result_published_version_number IS NOT NULL)",
            name="ck_workflow_control_operations_publication_shape",
        ),
        CheckConstraint(
            "(operation IN ('create','update','archive') AND result_draft_revision IS NOT NULL AND result_draft_checksum IS NOT NULL) OR "
            "(operation = 'save_draft' AND result_draft_revision IS NULL AND result_draft_checksum IS NOT NULL) OR "
            "(operation NOT IN ('create','update','archive','save_draft') AND result_draft_revision IS NULL AND result_draft_checksum IS NULL)",
            name="ck_workflow_control_operations_draft_shape",
        ),
        CheckConstraint(
            "(operation IN ('create','update','save_draft','archive','version_grant_put','version_grant_delete') AND result_revision IS NOT NULL) OR "
            "(operation NOT IN ('create','update','save_draft','archive','version_grant_put','version_grant_delete') AND result_revision IS NULL)",
            name="ck_workflow_control_operations_revision_shape",
        ),
        CheckConstraint(
            "(operation = 'version_grant_put' AND result_status IS NOT NULL AND result_status = 'active') OR "
            "(operation = 'version_grant_delete' AND result_status IS NOT NULL AND result_status = 'revoked') OR "
            "(operation NOT IN ('version_grant_put','version_grant_delete') AND result_status IS NULL)",
            name="ck_workflow_control_operations_status_shape",
        ),
        CheckConstraint(
            "(operation IN ('create','update','archive','version_grant_put','version_grant_delete') AND result_created_at IS NOT NULL) OR "
            "(operation NOT IN ('create','update','archive','version_grant_put','version_grant_delete') AND result_created_at IS NULL)",
            name="ck_workflow_control_operations_created_at_shape",
        ),
        CheckConstraint(
            "(operation IN ('create','update','save_draft','archive','draft_grant_put') AND result_updated_at IS NOT NULL) OR (operation NOT IN ('create','update','save_draft','archive','draft_grant_put') AND result_updated_at IS NULL)",
            name="ck_workflow_control_operations_updated_at_shape",
        ),
        CheckConstraint(
            "(operation = 'version_grant_delete' AND result_revoked_at IS NOT NULL) OR (operation <> 'version_grant_delete' AND result_revoked_at IS NULL)",
            name="ck_workflow_control_operations_revoked_at_shape",
        ),
        CheckConstraint(
            "(operation = 'publish' AND result_missing_slot_ids_csv IS NOT NULL) OR (operation <> 'publish' AND result_missing_slot_ids_csv IS NULL)",
            name="ck_workflow_control_operations_publish_shape",
        ),
        ForeignKeyConstraint(
            ["workflow_id", "project_id"],
            ["workflow_definitions.id", "workflow_definitions.project_id"],
            name="fk_workflow_control_operations_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["result_published_version_id", "workflow_id", "project_id"],
            [
                "workflow_versions.id",
                "workflow_versions.workflow_id",
                "workflow_versions.project_id",
            ],
            name="fk_workflow_control_operations_published_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["result_version_id", "workflow_id", "project_id"],
            [
                "workflow_versions.id",
                "workflow_versions.workflow_id",
                "workflow_versions.project_id",
            ],
            name="fk_workflow_control_operations_result_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_workflow_control_operations_actor",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_workflow_control_operations_project_created",
            "project_id",
            created_at.desc(),
        ),
    )


class WorkflowVersionModelRefRow(Base):
    __tablename__ = "workflow_version_model_refs"

    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    logical_model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        CheckConstraint("purpose ~ '^[a-z][a-z0-9._-]{0,63}$'", name="ck_workflow_version_model_refs_purpose"),
        CheckConstraint("logical_model_name = btrim(logical_model_name) AND logical_model_name <> ''", name="ck_workflow_version_model_refs_name"),
        UniqueConstraint(
            "workflow_version_id",
            "project_id",
            "node_id",
            "purpose",
            "logical_model_name",
            name="uq_workflow_version_model_refs_exact",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id"],
            ["workflow_versions.id", "workflow_versions.project_id"],
            name="fk_workflow_version_model_refs_version",
            ondelete="RESTRICT",
        ),
    )


class WorkflowDraftCredentialGrantIntentRow(Base):
    __tablename__ = "workflow_draft_credential_grant_intents"

    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    slot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    slot_schema_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    credential_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="project", server_default=text("'project'"))
    credential_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    expected_credential_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'", name="ck_workflow_draft_grant_intents_slot"),
        CheckConstraint("slot_schema_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_draft_grant_intents_checksum"),
        CheckConstraint("credential_scope = 'project'", name="ck_workflow_draft_grant_intents_scope"),
        ForeignKeyConstraint(
            ["workflow_id", "project_id"],
            ["workflow_definitions.id", "workflow_definitions.project_id"],
            name="fk_workflow_draft_grant_intents_definition",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["credential_id", "credential_scope"],
            ["credentials.id", "credentials.scope"],
            name="fk_workflow_draft_grant_intents_credential_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "credential_id"],
            ["credentials.project_id", "credentials.id"],
            name="fk_workflow_draft_grant_intents_project_credential",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["credential_id", "expected_credential_version_id"],
            ["credential_versions.credential_id", "credential_versions.id"],
            name="fk_workflow_draft_grant_intents_credential_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_workflow_draft_grant_intents_updated_by", ondelete="RESTRICT"),
    )


class WorkflowVersionCredentialSlotRow(Base):
    __tablename__ = "workflow_version_credential_slots"

    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    slot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_schema_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    payload_schema_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'", name="ck_workflow_version_credential_slots_slot"),
        CheckConstraint("purpose ~ '^[a-z][a-z0-9._-]{0,63}$'", name="ck_workflow_version_credential_slots_purpose"),
        CheckConstraint("jsonb_typeof(payload_schema_json) = 'object'", name="ck_workflow_version_credential_slots_schema_object"),
        CheckConstraint("payload_schema_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_version_credential_slots_checksum"),
        CheckConstraint("required", name="ck_workflow_version_credential_slots_required"),
        UniqueConstraint(
            "workflow_version_id",
            "slot_id",
            "payload_schema_checksum",
            name="uq_workflow_version_credential_slots_exact",
        ),
        UniqueConstraint(
            "workflow_version_id",
            "project_id",
            "slot_id",
            name="uq_workflow_version_credential_slots_scope",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id"],
            ["workflow_versions.id", "workflow_versions.project_id"],
            name="fk_workflow_version_credential_slots_version",
            ondelete="RESTRICT",
        ),
    )


class WorkflowVersionCodeRequirementRow(Base):
    __tablename__ = "workflow_version_code_requirements"

    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    runtime_contract: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "runtime_contract = 'python3.12-v1'",
            name="ck_workflow_version_code_requirements_contract",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id"],
            ["workflow_versions.id", "workflow_versions.project_id"],
            name="fk_workflow_version_code_requirements_version",
            ondelete="RESTRICT",
        ),
    )


class WorkflowVersionHttpRequirementRow(Base):
    __tablename__ = "workflow_version_http_requirements"

    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    endpoint_policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    injection_profile_id: Mapped[str | None] = mapped_column(String(128))
    credential_slot_id: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        CheckConstraint(
            "method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE')",
            name="ck_workflow_version_http_requirements_method",
        ),
        CheckConstraint(
            "endpoint_policy_id ~ '^[a-z][a-z0-9._-]{0,127}$'",
            name="ck_workflow_version_http_requirements_endpoint",
        ),
        CheckConstraint(
            "injection_profile_id IS NULL OR injection_profile_id ~ '^[a-z][a-z0-9._-]{0,127}$'",
            name="ck_workflow_version_http_requirements_injection",
        ),
        CheckConstraint(
            "credential_slot_id IS NULL OR credential_slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'",
            name="ck_workflow_version_http_requirements_slot",
        ),
        CheckConstraint(
            "(injection_profile_id IS NULL) = (credential_slot_id IS NULL)",
            name="ck_workflow_version_http_requirements_auth_pair",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id"],
            ["workflow_versions.id", "workflow_versions.project_id"],
            name="fk_workflow_version_http_requirements_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id", "credential_slot_id"],
            [
                "workflow_version_credential_slots.workflow_version_id",
                "workflow_version_credential_slots.project_id",
                "workflow_version_credential_slots.slot_id",
            ],
            name="fk_workflow_version_http_requirements_slot",
            ondelete="RESTRICT",
        ),
    )


class WorkflowCredentialGrantRow(Base):
    __tablename__ = "workflow_credential_grants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    slot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="project", server_default=text("'project'"))
    credential_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload_schema_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default=text("'active'"))
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    granted_by: Mapped[str] = mapped_column(String(36), nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("credential_scope = 'project'", name="ck_workflow_credential_grants_scope"),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_workflow_credential_grants_status"),
        CheckConstraint("revision >= 1", name="ck_workflow_credential_grants_revision"),
        CheckConstraint("payload_schema_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_credential_grants_checksum"),
        CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL AND revoked_by IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)",
            name="ck_workflow_credential_grants_lifecycle",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "workflow_version_id",
            "slot_id",
            "credential_id",
            "credential_version_id",
            "payload_schema_checksum",
            name="uq_workflow_credential_grants_exact",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id"],
            ["workflow_versions.id", "workflow_versions.project_id"],
            name="fk_workflow_credential_grants_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "slot_id", "payload_schema_checksum"],
            [
                "workflow_version_credential_slots.workflow_version_id",
                "workflow_version_credential_slots.slot_id",
                "workflow_version_credential_slots.payload_schema_checksum",
            ],
            name="fk_workflow_credential_grants_slot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["credential_id", "credential_scope"],
            ["credentials.id", "credentials.scope"],
            name="fk_workflow_credential_grants_credential_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "credential_id"],
            ["credentials.project_id", "credentials.id"],
            name="fk_workflow_credential_grants_project_credential",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["credential_id", "credential_version_id"],
            ["credential_versions.credential_id", "credential_versions.id"],
            name="fk_workflow_credential_grants_credential_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["granted_by"], ["users.id"], name="fk_workflow_credential_grants_granted_by", ondelete="RESTRICT"),
        ForeignKeyConstraint(["revoked_by"], ["users.id"], name="fk_workflow_credential_grants_revoked_by", ondelete="RESTRICT"),
        Index(
            "uq_workflow_credential_grants_active_slot",
            "workflow_version_id",
            "slot_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class WorkflowRunRow(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    output_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    input_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    admission_request_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(String(128))
    origin_trace_id: Mapped[str] = mapped_column(String(512), nullable=False)
    required_worker_profile_digest: Mapped[str | None] = mapped_column(CHAR(64))
    worker_profile_key: Mapped[str] = mapped_column(CHAR(64), nullable=False, default=_EMPTY_PROFILE_KEY, server_default=text(f"'{_EMPTY_PROFILE_KEY}'"))
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    current_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    retry_of_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','side_effect_unknown')",
            name="ck_workflow_runs_status",
        ),
        CheckConstraint("trigger_kind IN ('manual','api')", name="ck_workflow_runs_trigger"),
        CheckConstraint("execution_epoch >= 1", name="ck_workflow_runs_epoch"),
        CheckConstraint("jsonb_typeof(input_json) = 'object'", name="ck_workflow_runs_input_object"),
        CheckConstraint("output_json IS NULL OR jsonb_typeof(output_json) = 'object'", name="ck_workflow_runs_output_object"),
        CheckConstraint("input_digest ~ '^[0-9a-f]{64}$'", name="ck_workflow_runs_input_digest"),
        CheckConstraint("idempotency_hash ~ '^[0-9a-f]{64}$'", name="ck_workflow_runs_idempotency"),
        CheckConstraint(
            "admission_request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_workflow_runs_admission_request_digest",
        ),
        CheckConstraint(
            f"(required_worker_profile_digest IS NULL AND worker_profile_key = '{_EMPTY_PROFILE_KEY}') OR "
            "(required_worker_profile_digest IS NOT NULL "
            "AND required_worker_profile_digest ~ '^[0-9a-f]{64}$' "
            "AND worker_profile_key = required_worker_profile_digest)",
            name="ck_workflow_runs_profile_digest",
        ),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND completed_at IS NULL AND output_json IS NULL AND error_code IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND started_at >= created_at AND completed_at IS NULL AND output_json IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND started_at IS NOT NULL AND completed_at IS NOT NULL AND started_at >= created_at AND completed_at >= started_at AND output_json IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('failed','side_effect_unknown') AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND error_code IS NOT NULL "
            "AND started_at >= created_at AND completed_at >= started_at "
            "AND output_json IS NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,63}$') OR "
            "(status = 'cancelled' AND completed_at IS NOT NULL AND completed_at >= COALESCE(started_at, created_at) AND (started_at IS NULL OR started_at >= created_at) AND output_json IS NULL AND error_code IS NULL)",
            name="ck_workflow_runs_lifecycle",
        ),
        CheckConstraint("retry_of_run_id IS NULL OR retry_of_run_id <> id", name="ck_workflow_runs_retry_self"),
        UniqueConstraint("id", "project_id", "owner_user_id", name="uq_workflow_runs_scope"),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "workflow_version_id",
            name="uq_workflow_runs_scope_version",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "workflow_id",
            "workflow_version_id",
            name="uq_workflow_runs_retry_scope",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "workflow_version_id",
            "worker_profile_key",
            name="uq_workflow_runs_snapshot_scope",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "worker_profile_key",
            name="uq_workflow_runs_scope_profile",
        ),
        UniqueConstraint("id", "project_id", "owner_user_id", "origin_trace_id", name="uq_workflow_runs_trace_scope"),
        UniqueConstraint("id", "execution_epoch", name="uq_workflow_runs_epoch"),
        UniqueConstraint("id", "execution_epoch", "worker_profile_key", name="uq_workflow_runs_epoch_profile"),
        UniqueConstraint("project_id", "owner_user_id", "workflow_id", "idempotency_hash", name="uq_workflow_runs_idempotency"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_workflow_runs_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_workflow_runs_owner", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_workflow_runs_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workflow_id", "project_id"],
            ["workflow_definitions.id", "workflow_definitions.project_id"],
            name="fk_workflow_runs_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "workflow_id", "project_id"],
            ["workflow_versions.id", "workflow_versions.workflow_id", "workflow_versions.project_id"],
            name="fk_workflow_runs_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["retry_of_run_id", "project_id", "owner_user_id", "workflow_id", "workflow_version_id"],
            [
                "workflow_runs.id",
                "workflow_runs.project_id",
                "workflow_runs.owner_user_id",
                "workflow_runs.workflow_id",
                "workflow_runs.workflow_version_id",
            ],
            name="fk_workflow_runs_retry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["current_job_id", "project_id", "owner_user_id", "id", "execution_epoch", "worker_profile_key"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.workflow_run_id", "jobs.workflow_epoch", "jobs.workflow_profile_key"],
            name="fk_workflow_runs_current_job",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "execution_epoch"],
            [
                "workflow_run_jobs.workflow_run_id",
                "workflow_run_jobs.execution_epoch",
            ],
            name="fk_workflow_runs_epoch_mapping",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_workflow_runs_history", "project_id", "owner_user_id", created_at.desc(), id.desc()),
        Index(
            "ix_workflow_runs_active",
            "project_id",
            "owner_user_id",
            "status",
            postgresql_where=text("status IN ('queued','running')"),
        ),
    )


class WorkflowRunJobRow(Base):
    __tablename__ = "workflow_run_jobs"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    worker_profile_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    cause: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("execution_epoch >= 1", name="ck_workflow_run_jobs_epoch"),
        CheckConstraint("(cause = 'initial' AND execution_epoch = 1) OR (cause = 'resume' AND execution_epoch >= 2)", name="ck_workflow_run_jobs_cause"),
        CheckConstraint("worker_profile_key ~ '^[0-9a-f]{64}$'", name="ck_workflow_run_jobs_profile_key"),
        UniqueConstraint("job_id", name="uq_workflow_run_jobs_job"),
        UniqueConstraint("workflow_run_id", "execution_epoch", "job_id", name="uq_workflow_run_jobs_run_epoch_job"),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id", "worker_profile_key"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id", "workflow_runs.worker_profile_key"],
            name="fk_workflow_run_jobs_run_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["job_id", "project_id", "owner_user_id", "workflow_run_id", "execution_epoch", "worker_profile_key"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.workflow_run_id", "jobs.workflow_epoch", "jobs.workflow_profile_key"],
            name="fk_workflow_run_jobs_job_epoch",
            ondelete="RESTRICT",
        ),
    )


class WorkflowRunSnapshotRow(Base):
    __tablename__ = "workflow_run_snapshots"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    graph_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    compiler_contract_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    semantic_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    catalog_generation: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    required_worker_profile_digest: Mapped[str | None] = mapped_column(CHAR(64))
    worker_profile_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    snapshot_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("graph_schema_version >= 1 AND compiler_contract_version >= 1", name="ck_workflow_run_snapshots_schema"),
        CheckConstraint("catalog_generation ~ '^[0-9a-f]{64}$'", name="ck_workflow_run_snapshots_generation"),
        CheckConstraint("semantic_checksum ~ '^[0-9a-f]{64}$' AND snapshot_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_run_snapshots_checksums"),
        CheckConstraint(
            f"(required_worker_profile_digest IS NULL AND worker_profile_key = '{_EMPTY_PROFILE_KEY}') OR "
            "(required_worker_profile_digest IS NOT NULL "
            "AND required_worker_profile_digest ~ '^[0-9a-f]{64}$' "
            "AND worker_profile_key = required_worker_profile_digest)",
            name="ck_workflow_run_snapshots_profile_digest",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id", "workflow_version_id", "worker_profile_key"],
            [
                "workflow_runs.id",
                "workflow_runs.project_id",
                "workflow_runs.owner_user_id",
                "workflow_runs.workflow_version_id",
                "workflow_runs.worker_profile_key",
            ],
            name="fk_workflow_run_snapshots_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "workflow_version_id",
                "project_id",
                "graph_schema_version",
                "compiler_contract_version",
                "semantic_checksum",
            ],
            [
                "workflow_versions.id",
                "workflow_versions.project_id",
                "workflow_versions.graph_schema_version",
                "workflow_versions.compiler_contract_version",
                "workflow_versions.semantic_checksum",
            ],
            name="fk_workflow_run_snapshots_version",
            ondelete="RESTRICT",
        ),
    )


class WorkflowRunRuntimePolicySnapshotRow(Base):
    __tablename__ = "workflow_run_runtime_policy_snapshots"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    section: Mapped[str] = mapped_column(String(32), nullable=False, default="workflow_runtime", server_default=text("'workflow_runtime'"))
    policy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    value_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("section = 'workflow_runtime'", name="ck_workflow_run_runtime_policy_snapshots_section"),
        CheckConstraint("revision >= 1 AND schema_version >= 1", name="ck_workflow_run_runtime_policy_snapshots_versions"),
        CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_run_runtime_policy_snapshots_checksum"),
        CheckConstraint("jsonb_typeof(value_json) = 'object'", name="ck_workflow_run_runtime_policy_snapshots_value"),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id"],
            name="fk_workflow_run_runtime_policy_snapshots_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["section", "policy_version_id", "revision", "schema_version", "payload_checksum"],
            [
                "system_runtime_policy_versions.section",
                "system_runtime_policy_versions.id",
                "system_runtime_policy_versions.version_number",
                "system_runtime_policy_versions.schema_version",
                "system_runtime_policy_versions.payload_checksum",
            ],
            name="fk_workflow_run_runtime_policy_snapshots_exact_policy",
            ondelete="RESTRICT",
        ),
    )


class WorkflowRunModelSnapshotRow(Base):
    __tablename__ = "workflow_run_model_snapshots"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)
    logical_model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_config_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model_config_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    credential_env_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("purpose ~ '^[a-z][a-z0-9._-]{0,63}$'", name="ck_workflow_run_model_snapshots_purpose"),
        CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_workflow_run_model_snapshots_checksum"),
        CheckConstraint(
            "(credential_id IS NULL AND credential_version_id IS NULL AND credential_env_key IS NULL) OR (credential_id IS NOT NULL AND credential_version_id IS NOT NULL AND credential_env_key IS NOT NULL)",
            name="ck_workflow_run_model_snapshots_credential_group",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id", "workflow_version_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id", "workflow_runs.workflow_version_id"],
            name="fk_workflow_run_model_snapshots_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id", "node_id", "purpose", "logical_model_name"],
            [
                "workflow_version_model_refs.workflow_version_id",
                "workflow_version_model_refs.project_id",
                "workflow_version_model_refs.node_id",
                "workflow_version_model_refs.purpose",
                "workflow_version_model_refs.logical_model_name",
            ],
            name="fk_workflow_run_model_snapshots_model_ref",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["model_config_id", "model_config_version_id", "payload_checksum"],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
                "system_model_config_versions.payload_checksum",
            ],
            name="fk_workflow_run_model_snapshots_exact_model",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["model_config_id", "model_config_version_id", "payload_checksum", "credential_id", "credential_version_id", "credential_env_key"],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
                "system_model_config_versions.payload_checksum",
                "system_model_config_versions.credential_id",
                "system_model_config_versions.credential_version_id",
                "system_model_config_versions.credential_env_key",
            ],
            name="fk_workflow_run_model_snapshots_credential_closure",
            ondelete="RESTRICT",
        ),
    )


class WorkflowRunCodeSnapshotRow(Base):
    __tablename__ = "workflow_run_code_snapshots"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    runtime_name: Mapped[str] = mapped_column(String(32), nullable=False)
    runner_contract_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    isolation_policy_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    profile_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    timeout_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_output_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("runtime_name = 'python3.12'", name="ck_workflow_run_code_snapshots_runtime"),
        CheckConstraint("runner_contract_version >= 1", name="ck_workflow_run_code_snapshots_contract"),
        CheckConstraint("image_digest ~ '^sha256:[0-9a-f]{64}$'", name="ck_workflow_run_code_snapshots_image"),
        CheckConstraint("isolation_policy_checksum ~ '^[0-9a-f]{64}$' AND profile_digest ~ '^[0-9a-f]{64}$'", name="ck_workflow_run_code_snapshots_digests"),
        CheckConstraint("timeout_ms BETWEEN 1 AND 31536000000 AND max_output_bytes BETWEEN 1 AND 2147483648", name="ck_workflow_run_code_snapshots_limits"),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id"],
            name="fk_workflow_run_code_snapshots_run",
            ondelete="CASCADE",
        ),
    )


class WorkflowRunHttpSnapshotRow(Base):
    __tablename__ = "workflow_run_http_snapshots"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    http_method: Mapped[str] = mapped_column(String(6), nullable=False)
    normalized_origin: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    endpoint_policy_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    injection_profile_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    injection_profile_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    egress_profile_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    timeout_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_request_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_response_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credential_slot_id: Mapped[str | None] = mapped_column(String(128))
    credential_grant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    payload_schema_checksum: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("http_method IN ('GET','HEAD','POST','PUT','PATCH','DELETE')", name="ck_workflow_run_http_snapshots_method"),
        CheckConstraint("normalized_origin ~ '^https://[^/?#]+$'", name="ck_workflow_run_http_snapshots_origin"),
        CheckConstraint("endpoint_policy_revision >= 1 AND injection_profile_revision >= 1", name="ck_workflow_run_http_snapshots_revisions"),
        CheckConstraint(
            "endpoint_policy_checksum ~ '^[0-9a-f]{64}$' AND injection_profile_checksum ~ '^[0-9a-f]{64}$' AND egress_profile_digest ~ '^[0-9a-f]{64}$'",
            name="ck_workflow_run_http_snapshots_digests",
        ),
        CheckConstraint(
            "timeout_ms BETWEEN 1 AND 31536000000 AND max_request_bytes BETWEEN 0 AND 2147483648 AND max_response_bytes BETWEEN 1 AND 2097152",
            name="ck_workflow_run_http_snapshots_limits",
        ),
        CheckConstraint(
            "(credential_slot_id IS NULL AND credential_grant_id IS NULL "
            "AND credential_id IS NULL AND credential_version_id IS NULL "
            "AND payload_schema_checksum IS NULL) OR "
            "(credential_slot_id IS NOT NULL AND credential_grant_id IS NOT NULL "
            "AND credential_id IS NOT NULL AND credential_version_id IS NOT NULL "
            "AND payload_schema_checksum IS NOT NULL "
            "AND payload_schema_checksum ~ '^[0-9a-f]{64}$')",
            name="ck_workflow_run_http_snapshots_credential_group",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id", "workflow_version_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id", "workflow_runs.workflow_version_id"],
            name="fk_workflow_run_http_snapshots_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "project_id"],
            ["workflow_versions.id", "workflow_versions.project_id"],
            name="fk_workflow_run_http_snapshots_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "credential_slot_id", "payload_schema_checksum"],
            [
                "workflow_version_credential_slots.workflow_version_id",
                "workflow_version_credential_slots.slot_id",
                "workflow_version_credential_slots.payload_schema_checksum",
            ],
            name="fk_workflow_run_http_snapshots_slot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "credential_grant_id",
                "project_id",
                "workflow_version_id",
                "credential_slot_id",
                "credential_id",
                "credential_version_id",
                "payload_schema_checksum",
            ],
            [
                "workflow_credential_grants.id",
                "workflow_credential_grants.project_id",
                "workflow_credential_grants.workflow_version_id",
                "workflow_credential_grants.slot_id",
                "workflow_credential_grants.credential_id",
                "workflow_credential_grants.credential_version_id",
                "workflow_credential_grants.payload_schema_checksum",
            ],
            name="fk_workflow_run_http_snapshots_grant",
            ondelete="RESTRICT",
        ),
    )


class WorkflowCodeSandboxLeaseRow(Base):
    __tablename__ = "workflow_code_sandbox_leases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    activation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activation_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workflow_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reconciliation_key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    profile_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    execution_lease_token_hash: Mapped[str | None] = mapped_column(CHAR(64))
    cleanup_locator_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    cleanup_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleanup_handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_owner_worker_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    cleanup_lease_token_hash: Mapped[str | None] = mapped_column(CHAR(64))
    cleanup_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("workflow_run_id", "node_id", "activation_id", "activation_attempt", name="uq_workflow_code_leases_activation_attempt"),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id"],
            name="fk_workflow_code_leases_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["job_id", "job_attempt_number", "worker_id"],
            ["job_attempts.job_id", "job_attempts.attempt_number", "job_attempts.worker_id"],
            name="fk_workflow_code_leases_job_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["job_id", "project_id", "owner_user_id", "workflow_run_id", "workflow_epoch", "profile_digest"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.workflow_run_id", "jobs.workflow_epoch", "jobs.workflow_profile_key"],
            name="fk_workflow_code_leases_job_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "workflow_epoch", "job_id"],
            ["workflow_run_jobs.workflow_run_id", "workflow_run_jobs.execution_epoch", "workflow_run_jobs.job_id"],
            name="fk_workflow_code_leases_run_job_mapping",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "activation_attempt >= 1 AND workflow_epoch >= 1 AND job_attempt_number >= 1 AND cleanup_attempt >= 0 "
            "AND reconciliation_key_hash ~ '^[0-9a-f]{64}$' AND profile_digest ~ '^[0-9a-f]{64}$' AND ("
            "(state = 'provisioning' AND execution_lease_token_hash IS NOT NULL "
            "AND execution_lease_token_hash ~ '^[0-9a-f]{64}$' "
            "AND cleanup_locator_ciphertext IS NULL AND cleanup_handoff_at IS NULL "
            "AND cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL "
            "AND cleanup_lease_expires_at IS NULL AND destroyed_at IS NULL) OR "
            "(state = 'running' AND execution_lease_token_hash IS NOT NULL "
            "AND execution_lease_token_hash ~ '^[0-9a-f]{64}$' "
            "AND cleanup_locator_ciphertext IS NOT NULL "
            "AND octet_length(cleanup_locator_ciphertext) > 0 AND cleanup_handoff_at IS NULL "
            "AND cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL "
            "AND cleanup_lease_expires_at IS NULL AND destroyed_at IS NULL) OR "
            "(state = 'cleanup_pending' AND execution_lease_token_hash IS NULL "
            "AND cleanup_handoff_at IS NOT NULL AND destroyed_at IS NULL AND ("
            "(cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL "
            "AND cleanup_lease_expires_at IS NULL) OR "
            "(cleanup_owner_worker_id IS NOT NULL "
            "AND cleanup_lease_token_hash IS NOT NULL "
            "AND cleanup_lease_token_hash ~ '^[0-9a-f]{64}$' "
            "AND cleanup_lease_expires_at IS NOT NULL))) OR "
            "(state = 'destroyed' AND execution_lease_token_hash IS NULL "
            "AND cleanup_locator_ciphertext IS NULL AND cleanup_handoff_at IS NULL "
            "AND cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL "
            "AND cleanup_lease_expires_at IS NULL AND destroyed_at IS NOT NULL))",
            name="ck_workflow_code_leases_shape",
        ),
        Index(
            "uq_workflow_code_leases_open_activation",
            "workflow_run_id",
            "node_id",
            "activation_id",
            unique=True,
            postgresql_where=text("state <> 'destroyed'"),
        ),
        Index(
            "ix_workflow_code_leases_cleanup_claim",
            "state",
            "cleanup_lease_expires_at",
            "created_at",
            "id",
            postgresql_where=text("state IN ('provisioning','running','cleanup_pending')"),
        ),
    )


class WorkflowNodeEffectRow(Base):
    __tablename__ = "workflow_node_effects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    activation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    http_method: Mapped[str] = mapped_column(String(6), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    dispatch_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    dispatch_execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    dispatch_attempt: Mapped[int | None] = mapped_column(Integer)
    dispatch_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    dispatch_lease_token_hash: Mapped[str | None] = mapped_column(CHAR(64))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    outcome_digest: Mapped[str | None] = mapped_column(CHAR(64))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("http_method IN ('POST','PUT','PATCH','DELETE')", name="ck_workflow_node_effects_method"),
        CheckConstraint("status IN ('prepared','dispatching','settled','failed_safe','unknown')", name="ck_workflow_node_effects_status"),
        CheckConstraint("revision >= 1", name="ck_workflow_node_effects_revision"),
        CheckConstraint("request_hmac ~ '^[0-9a-f]{64}$'", name="ck_workflow_node_effects_request_hmac"),
        CheckConstraint("operation_key ~ '^[0-9a-f]{64}$'", name="ck_workflow_node_effects_operation_key"),
        CheckConstraint("provider_idempotency_key ~ '^[0-9a-f]{64}$'", name="ck_workflow_node_effects_provider_key"),
        CheckConstraint("(dispatch_execution_epoch IS NULL OR dispatch_execution_epoch >= 1) AND (dispatch_attempt IS NULL OR dispatch_attempt >= 1)", name="ck_workflow_node_effects_epoch_attempt"),
        CheckConstraint("dispatch_lease_token_hash IS NULL OR dispatch_lease_token_hash ~ '^[0-9a-f]{64}$'", name="ck_workflow_node_effects_lease_hash"),
        CheckConstraint("outcome_digest IS NULL OR outcome_digest ~ '^[0-9a-f]{64}$'", name="ck_workflow_node_effects_outcome_digest"),
        CheckConstraint("safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'", name="ck_workflow_node_effects_safe_error"),
        CheckConstraint(
            "(status = 'prepared' AND dispatch_job_id IS NULL "
            "AND dispatch_execution_epoch IS NULL AND dispatch_attempt IS NULL "
            "AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL "
            "AND dispatch_started_at IS NULL AND outcome_json IS NULL "
            "AND outcome_digest IS NULL AND safe_error_code IS NULL) OR "
            "(status = 'dispatching' AND dispatch_job_id IS NOT NULL "
            "AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL "
            "AND dispatch_owner_id IS NOT NULL AND dispatch_lease_token_hash IS NOT NULL "
            "AND dispatch_started_at IS NOT NULL AND outcome_json IS NULL "
            "AND outcome_digest IS NULL AND safe_error_code IS NULL) OR "
            "(status = 'settled' AND dispatch_job_id IS NOT NULL "
            "AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL "
            "AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL "
            "AND dispatch_started_at IS NOT NULL AND outcome_json IS NOT NULL "
            "AND workflow_http_settled_outcome_is_valid(outcome_json) "
            "AND outcome_digest IS NOT NULL AND safe_error_code IS NULL) OR "
            "(status = 'failed_safe' AND dispatch_job_id IS NOT NULL "
            "AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL "
            "AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL "
            "AND dispatch_started_at IS NOT NULL AND outcome_json IS NULL "
            "AND outcome_digest IS NULL AND safe_error_code IS NOT NULL "
            "AND safe_error_code <> 'SIDE_EFFECT_STATE_UNKNOWN') OR "
            "(status = 'unknown' AND dispatch_job_id IS NOT NULL "
            "AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL "
            "AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL "
            "AND dispatch_started_at IS NOT NULL AND outcome_json IS NULL "
            "AND outcome_digest IS NULL "
            "AND safe_error_code IS NOT NULL "
            "AND safe_error_code = 'SIDE_EFFECT_STATE_UNKNOWN')",
            name="ck_workflow_node_effects_state_shape",
        ),
        UniqueConstraint("workflow_run_id", "node_id", "activation_key", "operation_key", name="uq_workflow_node_effects_operation"),
        UniqueConstraint("workflow_run_id", "node_id", "activation_key", name="uq_workflow_node_effects_activation"),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id"],
            name="fk_workflow_node_effects_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dispatch_job_id", "project_id", "owner_user_id", "workflow_run_id", "dispatch_execution_epoch"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.workflow_run_id", "jobs.workflow_epoch"],
            name="fk_workflow_node_effects_dispatch_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dispatch_job_id", "dispatch_attempt"],
            ["job_attempts.job_id", "job_attempts.attempt_number"],
            name="fk_workflow_node_effects_dispatch_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dispatch_job_id", "dispatch_attempt", "dispatch_owner_id"],
            ["job_attempts.job_id", "job_attempts.attempt_number", "job_attempts.worker_id"],
            name="fk_workflow_node_effects_dispatch_worker",
            ondelete="RESTRICT",
        ),
    )


class WorkflowRunEventPartitionStateRow(Base):
    __tablename__ = "workflow_run_event_partition_state"

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True, server_default=text("true"))
    retained_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (CheckConstraint("singleton", name="ck_workflow_run_event_partition_state_singleton"),)


class WorkflowRunEventInvariantRow(Base):
    __tablename__ = "workflow_run_event_invariants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    activation_id: Mapped[str | None] = mapped_column(String(128))
    scope_path_hash: Mapped[str | None] = mapped_column(CHAR(64))
    iteration_path: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, default=list, server_default=text("'{}'::integer[]"))
    attempt: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("seq >= 1", name="ck_workflow_run_event_invariants_seq"),
        CheckConstraint(
            "(node_id IS NULL AND activation_id IS NULL AND scope_path_hash IS NULL AND attempt IS NULL AND cardinality(iteration_path) = 0) OR "
            "(node_id IS NOT NULL AND activation_id IS NOT NULL "
            "AND activation_id ~ '^[A-Za-z0-9._:-]+$' "
            "AND scope_path_hash IS NOT NULL "
            "AND scope_path_hash ~ '^[0-9a-f]{64}$' "
            "AND attempt IS NOT NULL AND attempt >= 1 "
            "AND cardinality(iteration_path) <= 16 "
            "AND array_position(iteration_path, NULL) IS NULL "
            "AND 0 < ALL(iteration_path))",
            name="ck_workflow_run_event_invariants_activation",
        ),
        UniqueConstraint("project_id", "owner_user_id", "workflow_run_id", "seq", name="uq_workflow_run_events_private_seq"),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id", "workflow_version_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id", "workflow_runs.workflow_version_id"],
            name="fk_workflow_run_event_invariants_run",
            ondelete="CASCADE",
        ),
        Index(
            "uq_workflow_run_events_terminal",
            "project_id",
            "owner_user_id",
            "workflow_run_id",
            unique=True,
            postgresql_where=text("is_terminal"),
        ),
        Index("ix_workflow_run_event_invariants_occurred_at", "occurred_at"),
        Index(
            "ix_workflow_run_event_invariants_activation_attempt",
            "workflow_run_id",
            "node_id",
            "activation_id",
            "scope_path_hash",
            "iteration_path",
            "attempt",
            postgresql_where=text("activation_id IS NOT NULL"),
        ),
    )


class WorkflowRunEventRow(Base):
    __tablename__ = "workflow_run_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    activation_id: Mapped[str | None] = mapped_column(String(128))
    scope_path_hash: Mapped[str | None] = mapped_column(CHAR(64))
    iteration_path: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, default=list, server_default=text("'{}'::integer[]"))
    attempt: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, nullable=False, default=_now)

    __table_args__ = (
        CheckConstraint("seq >= 1", name="ck_workflow_run_events_seq"),
        CheckConstraint(
            "event_type IN ('workflow.run.started','workflow.node.queued',"
            "'workflow.node.started','workflow.node.delta','workflow.node.log',"
            "'workflow.node.completed','workflow.node.failed',"
            "'workflow.run.completed','workflow.run.failed',"
            "'workflow.run.cancelled','workflow.run.side_effect_unknown')",
            name="ck_workflow_run_events_type",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_workflow_run_events_payload"),
        CheckConstraint("cardinality(iteration_path) <= 16 AND array_position(iteration_path, NULL) IS NULL AND 0 < ALL(iteration_path)", name="ck_workflow_run_events_iteration_path"),
        CheckConstraint(
            "(event_type LIKE 'workflow.node.%' AND node_id IS NOT NULL "
            "AND activation_id IS NOT NULL "
            "AND activation_id ~ '^[A-Za-z0-9._:-]+$' "
            "AND scope_path_hash IS NOT NULL "
            "AND scope_path_hash ~ '^[0-9a-f]{64}$' "
            "AND attempt IS NOT NULL AND attempt >= 1) OR "
            "(event_type LIKE 'workflow.run.%' AND node_id IS NULL AND activation_id IS NULL AND scope_path_hash IS NULL AND attempt IS NULL AND cardinality(iteration_path) = 0)",
            name="ck_workflow_run_events_activation",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "project_id", "owner_user_id", "workflow_version_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id", "workflow_runs.workflow_version_id"],
            name="fk_workflow_run_events_run",
            ondelete="CASCADE",
        ),
        Index("ix_workflow_run_events_replay", "workflow_run_id", "seq"),
        Index("ix_workflow_run_events_scope_time", "project_id", "owner_user_id", occurred_at.desc(), id.desc()),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )


@event.listens_for(WorkflowRunEventRow, "before_insert")
def _ensure_workflow_event_month_partition(_mapper, connection, target: WorkflowRunEventRow) -> None:
    if connection.dialect.name == "postgresql":
        if target.occurred_at is None:
            target.occurred_at = datetime.now(UTC)
        connection.execute(
            text("SELECT ensure_workflow_run_events_month_partition(:occurred_at)"),
            {"occurred_at": target.occurred_at},
        )


__all__ = [name for name in globals() if name.startswith("Workflow") and name.endswith("Row")]
