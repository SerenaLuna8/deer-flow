"""Version compatibility and compiler/snapshot identity contracts.

The contracts in this module classify an artifact only.  They never mutate a
Draft, Published Version, or Run Snapshot.  A Draft migration is possible only
when the caller supplies an explicit registered path; Published Versions and
Run Snapshots are immutable and therefore never consume such a path.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt, StrictStr, TypeAdapter, field_validator, model_validator

from deerflow.workflows import MAX_SAFE_JSON_INTEGER, StrictLiteralOne

_PositiveVersion = Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_JSON_INTEGER)]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_SafeIdentifier = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
_CANONICAL_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_canonical_uuid_input(value: object) -> object:
    if isinstance(value, str) and _CANONICAL_UUID_TEXT.fullmatch(value) is None:
        raise ValueError("UUID input must use canonical lowercase hyphenated text")
    return value


_CanonicalUuid = Annotated[uuid.UUID, BeforeValidator(_validate_canonical_uuid_input)]

type WorkflowArtifactKindV1 = Literal["draft", "published_version", "run_snapshot"]
type WorkflowReadOnlyReasonV1 = Literal[
    "NO_EXPLICIT_DRAFT_MIGRATION",
    "PUBLISHED_VERSION_MIGRATION_FORBIDDEN",
    "RUN_SNAPSHOT_MIGRATION_FORBIDDEN",
]


class _StrictCompatibilityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class WorkflowArtifactSchemaIdentityV1(_StrictCompatibilityContract):
    graph_schema_version: _PositiveVersion
    canvas_schema_version: _PositiveVersion
    compiler_contract_version: _PositiveVersion


class WorkflowSchemaMigrationPathV1(_StrictCompatibilityContract):
    migration_id: _SafeIdentifier
    source: WorkflowArtifactSchemaIdentityV1
    target: WorkflowArtifactSchemaIdentityV1

    @model_validator(mode="after")
    def require_real_transition(self) -> Self:
        if self.source == self.target:
            raise ValueError("a migration path must change the schema identity")
        return self


class WorkflowSchemaCurrentV1(_StrictCompatibilityContract):
    status: Literal["current"]
    artifact_kind: WorkflowArtifactKindV1
    identity: WorkflowArtifactSchemaIdentityV1
    read_only: StrictBool
    silent_upgrade_allowed: Literal[False]

    @field_validator("silent_upgrade_allowed", mode="before")
    @classmethod
    def require_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("silent upgrade flag must be a boolean")
        return value

    @model_validator(mode="after")
    def freeze_editability(self) -> Self:
        expected_read_only = self.artifact_kind != "draft"
        if self.read_only is not expected_read_only:
            raise ValueError("only a current Draft is writable")
        return self


class WorkflowSchemaMigratableV1(_StrictCompatibilityContract):
    status: Literal["migratable"]
    artifact_kind: Literal["draft"]
    source: WorkflowArtifactSchemaIdentityV1
    target: WorkflowArtifactSchemaIdentityV1
    migration_id: _SafeIdentifier
    requires_explicit_save: Literal[True]
    silent_upgrade_allowed: Literal[False]

    @field_validator("requires_explicit_save", "silent_upgrade_allowed", mode="before")
    @classmethod
    def require_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("migration flags must be booleans")
        return value

    @model_validator(mode="after")
    def require_real_transition(self) -> Self:
        if self.source == self.target:
            raise ValueError("a migratable Draft must change schema identity")
        return self


class WorkflowSchemaReadOnlyUnsupportedV1(_StrictCompatibilityContract):
    status: Literal["read_only_unsupported"]
    artifact_kind: WorkflowArtifactKindV1
    source: WorkflowArtifactSchemaIdentityV1
    supported: WorkflowArtifactSchemaIdentityV1
    reason: WorkflowReadOnlyReasonV1
    read_only: Literal[True]
    silent_upgrade_allowed: Literal[False]

    @field_validator("read_only", "silent_upgrade_allowed", mode="before")
    @classmethod
    def require_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("compatibility flags must be booleans")
        return value

    @model_validator(mode="after")
    def require_reason_for_artifact(self) -> Self:
        expected = {
            "draft": "NO_EXPLICIT_DRAFT_MIGRATION",
            "published_version": "PUBLISHED_VERSION_MIGRATION_FORBIDDEN",
            "run_snapshot": "RUN_SNAPSHOT_MIGRATION_FORBIDDEN",
        }[self.artifact_kind]
        if self.reason != expected:
            raise ValueError("read-only reason must match the artifact lifecycle")
        return self


type WorkflowSchemaCompatibilityV1 = Annotated[
    WorkflowSchemaCurrentV1 | WorkflowSchemaMigratableV1 | WorkflowSchemaReadOnlyUnsupportedV1,
    Field(discriminator="status"),
]
WORKFLOW_SCHEMA_COMPATIBILITY_V1_ADAPTER = TypeAdapter(WorkflowSchemaCompatibilityV1)


class WorkflowSchemaCompatibilityCaseV1(_StrictCompatibilityContract):
    """Cross-runtime fixture case; migration paths are declarations, not implementations."""

    artifact_kind: WorkflowArtifactKindV1
    source: WorkflowArtifactSchemaIdentityV1
    supported: WorkflowArtifactSchemaIdentityV1
    migration_paths: Annotated[tuple[WorkflowSchemaMigrationPathV1, ...], Field(max_length=32)]
    expected: WorkflowSchemaCompatibilityV1


WORKFLOW_SCHEMA_COMPATIBILITY_CASE_V1_ADAPTER = TypeAdapter(WorkflowSchemaCompatibilityCaseV1)


def assess_workflow_schema_compatibility(
    *,
    artifact_kind: WorkflowArtifactKindV1,
    source: WorkflowArtifactSchemaIdentityV1,
    supported: WorkflowArtifactSchemaIdentityV1,
    migration_paths: tuple[WorkflowSchemaMigrationPathV1, ...],
) -> WorkflowSchemaCompatibilityV1:
    """Classify without mutating or silently upgrading the artifact."""

    if type(source) is not WorkflowArtifactSchemaIdentityV1 or type(supported) is not WorkflowArtifactSchemaIdentityV1:
        raise TypeError("validated schema identities are required")
    if any(type(path) is not WorkflowSchemaMigrationPathV1 for path in migration_paths):
        raise TypeError("validated migration paths are required")

    if source == supported:
        return WorkflowSchemaCurrentV1(
            status="current",
            artifact_kind=artifact_kind,
            identity=source,
            read_only=artifact_kind != "draft",
            silent_upgrade_allowed=False,
        )

    if artifact_kind == "draft":
        matching = [path for path in migration_paths if path.source == source and path.target == supported]
        if len(matching) > 1:
            raise ValueError("multiple migration paths match the same schema transition")
        if matching:
            return WorkflowSchemaMigratableV1(
                status="migratable",
                artifact_kind="draft",
                source=source,
                target=supported,
                migration_id=matching[0].migration_id,
                requires_explicit_save=True,
                silent_upgrade_allowed=False,
            )
        reason: WorkflowReadOnlyReasonV1 = "NO_EXPLICIT_DRAFT_MIGRATION"
    elif artifact_kind == "published_version":
        reason = "PUBLISHED_VERSION_MIGRATION_FORBIDDEN"
    elif artifact_kind == "run_snapshot":
        reason = "RUN_SNAPSHOT_MIGRATION_FORBIDDEN"
    else:
        raise ValueError("unsupported Workflow artifact kind")

    return WorkflowSchemaReadOnlyUnsupportedV1(
        status="read_only_unsupported",
        artifact_kind=artifact_kind,
        source=source,
        supported=supported,
        reason=reason,
        read_only=True,
        silent_upgrade_allowed=False,
    )


class WorkflowCompilerContractIdentityV1(_StrictCompatibilityContract):
    graph_schema_version: _PositiveVersion
    compiler_contract_version: _PositiveVersion
    semantic_checksum: _Sha256Hex

    @property
    def cache_key(self) -> tuple[int, int, str]:
        return (self.graph_schema_version, self.compiler_contract_version, self.semantic_checksum)


class WorkflowRunSnapshotIdentityV1(_StrictCompatibilityContract):
    schema_version: StrictLiteralOne
    workflow_run_id: _CanonicalUuid
    workflow_version_id: _CanonicalUuid
    graph_schema_version: _PositiveVersion
    compiler_contract_version: _PositiveVersion
    semantic_checksum: _Sha256Hex
    catalog_generation: _SafeIdentifier
    snapshot_checksum: _Sha256Hex


class WorkflowCompilerSnapshotContractV1(_StrictCompatibilityContract):
    schema_version: StrictLiteralOne
    compiler_identity: WorkflowCompilerContractIdentityV1
    snapshot_identity: WorkflowRunSnapshotIdentityV1

    @model_validator(mode="after")
    def require_exact_compiler_identity(self) -> Self:
        snapshot_compiler_identity = (
            self.snapshot_identity.graph_schema_version,
            self.snapshot_identity.compiler_contract_version,
            self.snapshot_identity.semantic_checksum,
        )
        if snapshot_compiler_identity != self.compiler_identity.cache_key:
            raise ValueError("Run Snapshot must retain the exact compiler contract identity")
        return self


WORKFLOW_COMPILER_SNAPSHOT_CONTRACT_V1_ADAPTER = TypeAdapter(WorkflowCompilerSnapshotContractV1)


__all__ = [
    "WORKFLOW_COMPILER_SNAPSHOT_CONTRACT_V1_ADAPTER",
    "WORKFLOW_SCHEMA_COMPATIBILITY_CASE_V1_ADAPTER",
    "WORKFLOW_SCHEMA_COMPATIBILITY_V1_ADAPTER",
    "WorkflowArtifactKindV1",
    "WorkflowArtifactSchemaIdentityV1",
    "WorkflowCompilerContractIdentityV1",
    "WorkflowCompilerSnapshotContractV1",
    "WorkflowReadOnlyReasonV1",
    "WorkflowRunSnapshotIdentityV1",
    "WorkflowSchemaCompatibilityCaseV1",
    "WorkflowSchemaCompatibilityV1",
    "WorkflowSchemaCurrentV1",
    "WorkflowSchemaMigratableV1",
    "WorkflowSchemaMigrationPathV1",
    "WorkflowSchemaReadOnlyUnsupportedV1",
    "assess_workflow_schema_compatibility",
]
