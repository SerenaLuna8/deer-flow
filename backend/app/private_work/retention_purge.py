"""Transactional retention purge for expired private-work data."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, exists, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.sinks import TrustedOperationAuditSink
from app.quotas.integration import ProjectQuotaEnforcer
from deerflow.persistence.private_work.memory_v2_management import (
    MemoryV2ManagementRepository,
)
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
    RunSkillCredentialSnapshotRow,
    UserProjectMemoryFactRow,
    UserProjectMemoryRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentDesignSessionRow,
    SkillDesignSessionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user.model import UserRow
from deerflow.runtime.private_scope import PrivateResourceScope

_PURGE_NAMESPACE = uuid.UUID("1960a83e-df43-4f8c-85f4-b7193c08a9d0")


class RetentionNotEligible(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RETENTION_NOT_ELIGIBLE")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retention timestamp must be timezone-aware")
    return value.astimezone(UTC)


def retention_purge_id(idempotency_key: str) -> uuid.UUID:
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("retention idempotency key is required")
    return uuid.uuid5(_PURGE_NAMESPACE, idempotency_key)


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    resource_kind: str
    project_id: uuid.UUID | None
    owner_user_id: str | None
    membership_id: uuid.UUID | None
    activation_generation: int | None
    project_ids: tuple[uuid.UUID, ...]
    eligibility_at: datetime
    idempotency_key: str
    request_id: str
    early_delete: bool = False

    def __post_init__(self) -> None:
        if self.resource_kind not in {
            "project",
            "account",
            "former_owner",
        }:
            raise ValueError("invalid retention resource kind")
        if not isinstance(self.idempotency_key, str) or not 1 <= len(self.idempotency_key) <= 256:
            raise ValueError("invalid retention idempotency key")
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128:
            raise ValueError("invalid retention request id")
        object.__setattr__(self, "eligibility_at", _aware(self.eligibility_at))
        if type(self.early_delete) is not bool:
            raise TypeError("early_delete must be a boolean")

    @classmethod
    def project(
        cls,
        *,
        project_id: uuid.UUID,
        deletion_effective_at: datetime,
        idempotency_key: str,
        request_id: str,
    ) -> RetentionCandidate:
        return cls(
            "project",
            uuid.UUID(str(project_id)),
            None,
            None,
            None,
            (),
            deletion_effective_at,
            idempotency_key,
            request_id,
        )

    @classmethod
    def account(
        cls,
        *,
        owner_user_id: str,
        project_ids: tuple[uuid.UUID, ...],
        retention_until: datetime,
        idempotency_key: str,
        request_id: str,
    ) -> RetentionCandidate:
        projects = tuple(sorted({uuid.UUID(str(value)) for value in project_ids}, key=str))
        if not projects:
            raise ValueError("account purge requires retained project scopes")
        return cls(
            "account",
            None,
            str(uuid.UUID(str(owner_user_id))),
            None,
            None,
            projects,
            retention_until,
            idempotency_key,
            request_id,
        )

    @classmethod
    def former_owner(
        cls,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        membership_id: uuid.UUID,
        activation_generation: int,
        retention_until: datetime,
        idempotency_key: str,
        request_id: str,
        early_delete: bool = False,
        eligibility_at: datetime | None = None,
    ) -> RetentionCandidate:
        if not isinstance(activation_generation, int) or activation_generation < 1:
            raise ValueError("activation_generation must be positive")
        return cls(
            "former_owner",
            uuid.UUID(str(project_id)),
            str(uuid.UUID(str(owner_user_id))),
            uuid.UUID(str(membership_id)),
            activation_generation,
            (),
            eligibility_at or retention_until,
            idempotency_key,
            request_id,
            early_delete,
        )


@dataclass(frozen=True, slots=True)
class RetentionPurgeResult:
    purge_id: uuid.UUID
    resource_kind: str
    purged_count: int
    purged_at: datetime


class RetentionPurgeRepository:
    """Session-bound validation and deletion without transaction ownership."""

    async def verify_still_eligible(
        self,
        session: AsyncSession,
        candidate: RetentionCandidate,
        *,
        now: datetime,
    ) -> tuple[tuple[uuid.UUID, str | None], ...]:
        now = _aware(now)
        if now < candidate.eligibility_at:
            raise RetentionNotEligible
        if candidate.resource_kind == "project":
            assert candidate.project_id is not None
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == candidate.project_id).with_for_update())
            if project is None or project.status != "pending_deletion" or project.deletion_effective_at is None or _aware(project.deletion_effective_at) != candidate.eligibility_at or _aware(project.deletion_effective_at) > now:
                raise RetentionNotEligible
            memberships = (
                (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.project_id == candidate.project_id).order_by(ProjectMembershipRow.project_id, ProjectMembershipRow.user_id).with_for_update())).scalars().all()
            )
            return tuple((candidate.project_id, membership.user_id) for membership in memberships)

        if candidate.resource_kind == "former_owner":
            assert candidate.project_id is not None
            assert candidate.owner_user_id is not None
            assert candidate.membership_id is not None
            assert candidate.activation_generation is not None
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == candidate.project_id).with_for_update())
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.id == candidate.membership_id,
                    ProjectMembershipRow.project_id == candidate.project_id,
                    ProjectMembershipRow.user_id == candidate.owner_user_id,
                )
                .with_for_update()
            )
            if project is None or membership is None or membership.status not in {"left", "removed"} or membership.retention_until is None or membership.activation_generation != candidate.activation_generation:
                raise RetentionNotEligible
            retention_until = _aware(membership.retention_until)
            if candidate.early_delete:
                if candidate.eligibility_at > now:
                    raise RetentionNotEligible
            else:
                project_deadline = None if project.deletion_effective_at is None else _aware(project.deletion_effective_at)
                project_allows_owner_deadline = project.status == "active" or (project.status == "pending_deletion" and project_deadline is not None and retention_until < project_deadline)
                if not project_allows_owner_deadline or retention_until != candidate.eligibility_at or retention_until > now:
                    # The earlier deadline owns deletion. Equal deadlines are
                    # project-owned so only one exact purge case completes.
                    raise RetentionNotEligible
            return ((candidate.project_id, candidate.owner_user_id),)

        assert candidate.owner_user_id is not None
        owner = await session.scalar(select(UserRow).where(UserRow.id == candidate.owner_user_id).with_for_update())
        if owner is None:
            raise RetentionNotEligible
        projects = (await session.execute(select(ProjectRow).where(ProjectRow.id.in_(candidate.project_ids)).order_by(ProjectRow.id).with_for_update())).scalars().all()
        if tuple(sorted((project.id for project in projects), key=str)) != candidate.project_ids:
            raise RetentionNotEligible
        memberships = (
            (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.user_id == candidate.owner_user_id).order_by(ProjectMembershipRow.project_id, ProjectMembershipRow.user_id).with_for_update())).scalars().all()
        )
        actual_projects = tuple(sorted((membership.project_id for membership in memberships), key=str))
        if actual_projects != candidate.project_ids or not memberships:
            raise RetentionNotEligible
        if any(membership.status == "active" or membership.retention_until is None or _aware(membership.retention_until) > now for membership in memberships):
            raise RetentionNotEligible
        maximum_retention = max(_aware(membership.retention_until) for membership in memberships if membership.retention_until is not None)
        if maximum_retention != candidate.eligibility_at:
            raise RetentionNotEligible
        return tuple((membership.project_id, candidate.owner_user_id) for membership in memberships)

    async def physically_purge(
        self,
        session: AsyncSession,
        candidate: RetentionCandidate,
        *,
        quota: ProjectQuotaEnforcer,
    ) -> int:
        if candidate.resource_kind == "project":
            assert candidate.project_id is not None
            await release_private_storage_quota(
                session,
                project_id=candidate.project_id,
                owner_user_id=None,
                quota=quota,
                request_id=candidate.request_id,
            )
            await release_project_skill_storage_quota(
                session,
                project_id=candidate.project_id,
                quota=quota,
                request_id=candidate.request_id,
            )
            await purge_private_scope(session, project_id=candidate.project_id, owner_user_id=None)
            await purge_project_shared_scope(
                session,
                project_id=candidate.project_id,
            )
            await quota.reconcile_project_storage(
                session,
                candidate.project_id,
            )
            return 1
        if candidate.resource_kind == "former_owner":
            assert candidate.project_id is not None
            assert candidate.owner_user_id is not None
            await release_private_storage_quota(
                session,
                project_id=candidate.project_id,
                owner_user_id=candidate.owner_user_id,
                quota=quota,
                request_id=candidate.request_id,
            )
            await purge_private_scope(
                session,
                project_id=candidate.project_id,
                owner_user_id=candidate.owner_user_id,
            )
            return 1
        assert candidate.owner_user_id is not None
        for project_id in candidate.project_ids:
            await release_private_storage_quota(
                session,
                project_id=project_id,
                owner_user_id=candidate.owner_user_id,
                quota=quota,
                request_id=candidate.request_id,
            )
            await purge_private_scope(
                session,
                project_id=project_id,
                owner_user_id=candidate.owner_user_id,
            )
        return len(candidate.project_ids)


async def release_private_storage_quota(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str | None,
    quota: ProjectQuotaEnforcer,
    request_id: str,
) -> None:
    """Release exact ready-file reservations before their retention purge."""

    statement = (
        select(
            PrivateFileRow.id,
            PrivateFileRow.owner_user_id,
            PrivateFileRow.size,
            ProjectMembershipRow.version.label("membership_version"),
        )
        .join(
            ProjectMembershipRow,
            (ProjectMembershipRow.project_id == PrivateFileRow.project_id) & (ProjectMembershipRow.user_id == PrivateFileRow.owner_user_id),
        )
        .where(
            PrivateFileRow.project_id == project_id,
            PrivateFileRow.status == "ready",
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
        .order_by(PrivateFileRow.owner_user_id, PrivateFileRow.id)
        .with_for_update(of=PrivateFileRow)
    )
    rows = (await session.execute(statement)).all()
    for row in rows:
        await quota.release_file(
            session,
            PrivateResourceScope(
                project_id=str(project_id),
                owner_user_id=row.owner_user_id,
                membership_version=row.membership_version,
            ),
            file_id=row.id,
            size=row.size,
            request_id=request_id,
        )


async def release_project_skill_storage_quota(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    quota: ProjectQuotaEnforcer,
    request_id: str,
) -> None:
    """Release exact immutable Skill-version reservations before project purge."""

    rows = (
        await session.execute(
            select(
                SkillVersionFileRow.skill_version_id,
                SkillVersionFileRow.size_bytes,
            )
            .select_from(SkillVersionFileRow)
            .join(
                SkillVersionRow,
                SkillVersionRow.id == SkillVersionFileRow.skill_version_id,
            )
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillRow.scope == "project",
                SkillRow.project_id == project_id,
            )
            .order_by(
                SkillVersionFileRow.skill_version_id,
                SkillVersionFileRow.path,
            )
            .with_for_update(of=SkillVersionFileRow)
        )
    ).all()
    version_sizes: dict[uuid.UUID, int] = {}
    for row in rows:
        version_sizes[row.skill_version_id] = version_sizes.get(row.skill_version_id, 0) + row.size_bytes
    for version_id, size in version_sizes.items():
        await quota.release_skill_version_if_reserved(
            session,
            project_id=project_id,
            version_id=version_id,
            size=size,
        )


async def purge_private_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str | None,
) -> None:
    """Delete/scrub private payload for one exact project or project+owner scope.

    Immutable jobs, audit rows, governance rows, and their minimal FK shells are
    retained.  Rows with no immutable references are physically removed.
    """

    parameters: dict[str, object] = {"project_id": project_id, "purged_at": datetime.now(UTC)}
    owner_clause = ""
    if owner_user_id is not None:
        owner_clause = " AND owner_user_id = :owner_user_id"
        parameters["owner_user_id"] = owner_user_id

    def owner_for(alias: str) -> str:
        return "" if owner_user_id is None else f" AND {alias}.owner_user_id = :owner_user_id"

    # Agent Builder sessions contain private conversation and generated
    # blueprint bodies.  Delete the exact project/owner scope before shared
    # Agent versions are purged; operations cascade from the session row and
    # completed sessions otherwise retain RESTRICT references to their created
    # Agent/version.
    await session.execute(
        delete(AgentDesignSessionRow).where(
            AgentDesignSessionRow.project_id == project_id,
            *(() if owner_user_id is None else (AgentDesignSessionRow.owner_user_id == owner_user_id,)),
        )
    )
    # Skill Builder stores the same owner-private conversation class plus
    # temporary candidate BLOBs. Operations and files cascade with the session.
    # Completed sessions must be removed before their created Skill/version so
    # the retention transaction cannot be blocked by the intentional RESTRICT
    # foreign keys.
    await session.execute(
        delete(SkillDesignSessionRow).where(
            SkillDesignSessionRow.project_id == project_id,
            *(() if owner_user_id is None else (SkillDesignSessionRow.owner_user_id == owner_user_id,)),
        )
    )

    # Connection credentials/conversations cascade from exact connection rows.
    await session.execute(
        text(f"DELETE FROM channel_oauth_states WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )
    await session.execute(
        text(f"DELETE FROM channel_conversations WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )
    await session.execute(
        text(f"DELETE FROM channel_connections WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )

    await MemoryV2ManagementRepository(session).purge_scope(
        project_id=project_id,
        owner_user_id=owner_user_id,
        now=parameters["purged_at"],
    )

    await session.execute(
        delete(UserProjectMemoryFactRow).where(
            UserProjectMemoryFactRow.project_id == project_id,
            *(() if owner_user_id is None else (UserProjectMemoryFactRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(UserProjectMemoryRow).where(
            UserProjectMemoryRow.project_id == project_id,
            *(() if owner_user_id is None else (UserProjectMemoryRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunMcpGrantSnapshotRow).where(
            RunMcpGrantSnapshotRow.project_id == project_id,
            *(() if owner_user_id is None else (RunMcpGrantSnapshotRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunSkillCredentialSnapshotRow).where(
            RunSkillCredentialSnapshotRow.project_id == project_id,
            *(() if owner_user_id is None else (RunSkillCredentialSnapshotRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunAssetVersionRow).where(
            RunAssetVersionRow.project_id == project_id,
            *(() if owner_user_id is None else (RunAssetVersionRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(PrivateArtifactRow).where(
            PrivateArtifactRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateArtifactRow.owner_user_id == owner_user_id,)),
        )
    )
    file_ids = select(PrivateFileRow.id).where(
        PrivateFileRow.project_id == project_id,
        *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
    )
    await session.execute(delete(PrivateFileChunkRow).where(PrivateFileChunkRow.file_id.in_(file_ids)))
    await session.execute(
        update(PrivateFileRow)
        .where(
            PrivateFileRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
        .values(source_file_id=None)
    )
    await session.execute(
        delete(PrivateFileRow).where(
            PrivateFileRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
    )

    # Checkpoint tables are LangGraph-owned and intentionally addressed only by
    # the exact private Thread coordinates collected in this scope.
    thread_predicate = "project_id=:project_id" + owner_clause
    thread_ids = f"SELECT thread_id FROM threads_meta WHERE {thread_predicate}"
    for checkpoint_table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        if await session.scalar(text("SELECT to_regclass(:table_name)"), {"table_name": checkpoint_table}) is not None:
            await session.execute(
                text(f"DELETE FROM {checkpoint_table} WHERE thread_id IN ({thread_ids})"),
                parameters,
            )

    await session.execute(text(f"DELETE FROM run_events WHERE project_id=:project_id{owner_clause}"), parameters)
    await session.execute(text(f"DELETE FROM feedback WHERE project_id=:project_id{owner_clause}"), parameters)

    # Automation rows with immutable job references retain a scrubbed shell;
    # unreferenced rows are physically removed.
    await session.execute(
        text(
            f"""DELETE FROM scheduled_task_runs occurrence
                 WHERE occurrence.project_id=:project_id{owner_for("occurrence")}
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.automation_occurrence_id=occurrence.id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM scheduled_tasks task
                 WHERE task.project_id=:project_id{owner_for("task")}
                   AND NOT EXISTS (SELECT 1 FROM scheduled_task_runs occurrence WHERE occurrence.task_id=task.id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE scheduled_tasks task
                    SET title='purged', prompt='', schedule_spec='{{}}'::json,
                        status='cancelled', next_run_at=NULL, deleted_at=:purged_at,
                        updated_at=:purged_at
                  WHERE task.project_id=:project_id{owner_for("task")}"""
        ),
        parameters,
    )

    # Runs referenced by immutable jobs/audit are scrubbed, while unreferenced
    # Runs and then empty Thread shells are physically removed.
    await session.execute(
        text(
            f"""DELETE FROM runs run
                 WHERE run.project_id=:project_id{owner_for("run")}
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.run_id=run.run_id AND jobs.project_id=run.project_id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE runs run
                    SET assistant_id=NULL, metadata_json='{{}}'::json, kwargs_json='{{}}'::json,
                        error=NULL, first_human_message=NULL, last_ai_message=NULL
                  WHERE run.project_id=:project_id{owner_for("run")}"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM threads_meta thread
                 WHERE thread.project_id=:project_id{owner_for("thread")}
                   AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.thread_id=thread.thread_id)
                   AND NOT EXISTS (SELECT 1 FROM scheduled_tasks WHERE scheduled_tasks.thread_id=thread.thread_id)
                   AND NOT EXISTS (SELECT 1 FROM channel_conversations WHERE channel_conversations.thread_id=thread.thread_id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE threads_meta thread
                    SET assistant_id=NULL, display_name=NULL, metadata_json='{{}}'::json,
                        frozen_at=:purged_at, deleted_at=:purged_at,
                        checkpoint_delete_status='complete', updated_at=:purged_at
                  WHERE thread.project_id=:project_id{owner_for("thread")}"""
        ),
        parameters,
    )


async def _delete_project_version_leaves(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    asset_table: str,
    version_table: str,
    asset_id_column: str,
) -> None:
    """Delete an exact project's immutable version chain from leaves to root."""

    if (asset_table, version_table, asset_id_column) not in {
        ("agents", "agent_versions", "agent_id"),
        ("skills", "skill_versions", "skill_id"),
        ("mcp_servers", "mcp_server_versions", "mcp_server_id"),
        ("credentials", "credential_versions", "credential_id"),
    }:
        raise ValueError("unsupported project asset version chain")
    while True:
        deleted = (
            (
                await session.execute(
                    text(
                        f"""DELETE FROM {version_table} AS version
                         USING {asset_table} AS asset
                         WHERE version.{asset_id_column}=asset.id
                           AND asset.scope='project'
                           AND asset.project_id=:project_id
                           AND NOT EXISTS (
                               SELECT 1 FROM {version_table} AS child
                               WHERE child.supersedes_version_id=version.id
                           )
                         RETURNING version.id"""
                    ),
                    {"project_id": project_id},
                )
            )
            .scalars()
            .all()
        )
        if not deleted:
            return


async def purge_project_channel_guest_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> None:
    """Remove one project's group bindings and unreferenced guest principals.

    Project retention normally enters this boundary after ``purge_private_scope``.
    The explicit connection cleanup also makes the shared-scope purge safe when a
    recovery/operator path resumes after the private phase already committed or
    was skipped.  Human memberships and every other project are out of scope.

    Immutable job, Run, or audit shells may still reference a guest membership or
    user.  Those governance references win: nested savepoints turn the final
    membership/user removal into a safe orphan cleanup rather than weakening or
    deleting retained records.
    """

    project_uuid = uuid.UUID(str(project_id))
    guest_memberships = (
        await session.execute(
            select(
                ProjectMembershipRow.id.label("membership_id"),
                ProjectMembershipRow.user_id,
            )
            .where(
                ProjectMembershipRow.project_id == project_uuid,
                ProjectMembershipRow.role == "channel_guest",
            )
            .order_by(ProjectMembershipRow.user_id, ProjectMembershipRow.id)
            .with_for_update(of=ProjectMembershipRow)
        )
    ).all()
    guest_user_ids = tuple(sorted({row.user_id for row in guest_memberships}))
    parameters: dict[str, object] = {"project_id": project_uuid}
    if guest_user_ids:
        parameters["guest_user_ids"] = list(guest_user_ids)
        guest_owner_clause = "owner_user_id = ANY(CAST(:guest_user_ids AS varchar[]))"
        # OAuth rows are not expected for non-login principals, but deleting
        # them makes the boundary fail closed if malformed legacy data exists.
        await session.execute(
            text(f"DELETE FROM channel_oauth_states WHERE project_id=:project_id AND {guest_owner_clause}"),
            parameters,
        )
        await session.execute(
            text(f"DELETE FROM channel_conversations WHERE project_id=:project_id AND {guest_owner_clause}"),
            parameters,
        )
        await session.execute(
            text(f"DELETE FROM channel_connections WHERE project_id=:project_id AND {guest_owner_clause}"),
            parameters,
        )

    # Challenges and bindings retain RESTRICT references to Agent rows, so they
    # must be removed before the shared asset version chains are deleted.
    await session.execute(
        text("DELETE FROM project_channel_group_binding_challenges WHERE project_id=:project_id"),
        parameters,
    )
    await session.execute(
        text("DELETE FROM channel_external_principals WHERE project_id=:project_id"),
        parameters,
    )
    await session.execute(
        text("DELETE FROM project_channel_group_bindings WHERE project_id=:project_id"),
        parameters,
    )

    for row in guest_memberships:
        try:
            async with session.begin_nested():
                await session.execute(
                    delete(ProjectMembershipRow).where(
                        ProjectMembershipRow.id == row.membership_id,
                        ProjectMembershipRow.project_id == project_uuid,
                        ProjectMembershipRow.user_id == row.user_id,
                        ProjectMembershipRow.role == "channel_guest",
                    )
                )
                await session.flush()
        except IntegrityError:
            # A retained immutable shell still owns this exact membership.
            continue

    for guest_user_id in guest_user_ids:
        try:
            async with session.begin_nested():
                await session.execute(
                    delete(UserRow).where(
                        UserRow.id == guest_user_id,
                        UserRow.principal_type == "channel_guest",
                        ~exists(select(ProjectMembershipRow.id).where(ProjectMembershipRow.user_id == guest_user_id)),
                    )
                )
                await session.flush()
        except IntegrityError:
            # Audit/governance rows may legitimately retain a pseudonymous
            # guest principal. Never delete those references or a human user.
            continue


async def purge_project_shared_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> None:
    """Irreversibly remove one deleted project's shared assets and secrets.

    The project, memberships, immutable jobs, and audit rows remain as bounded
    governance tombstones.  Project Agent rows that are still required by a
    retained Thread/Automation FK are reduced to a content-free shell; every
    version body is physically removed.
    """

    project_uuid = uuid.UUID(str(project_id))
    parameters = {
        "project_id": project_uuid,
        "purged_at": datetime.now(UTC),
    }

    await purge_project_channel_guest_scope(
        session,
        project_id=project_uuid,
    )

    # The default pointer must be removed before project Agent packages can be
    # physically deleted or reduced to retained shells.
    await session.execute(
        text("DELETE FROM project_default_agents WHERE project_id=:project_id"),
        parameters,
    )

    for table_name in (
        "project_system_agent_bindings",
        "project_system_skill_bindings",
        "project_system_mcp_bindings",
    ):
        await session.execute(
            text(f"DELETE FROM {table_name} WHERE project_id=:project_id"),
            parameters,
        )

    # Run Skill credential snapshots were removed with private work. Removing
    # the project-local config now cascades both active and revoked binding
    # history, including configurations for packaged System Skills.
    await session.execute(
        text("DELETE FROM project_skill_credential_configs WHERE project_id=:project_id"),
        parameters,
    )

    await session.execute(
        text("DELETE FROM project_invitations WHERE project_id=:project_id"),
        parameters,
    )

    # Retention is a physical purge boundary, so it intentionally traverses
    # both visible and logically deleted Credentials.
    project_credential_versions = """SELECT version.id
        FROM credential_versions AS version
        JOIN credentials AS asset ON asset.id=version.credential_id
        WHERE asset.scope='project' AND asset.project_id=:project_id"""
    project_mcp_versions = """SELECT version.id
        FROM mcp_server_versions AS version
        JOIN mcp_servers AS asset ON asset.id=version.mcp_server_id
        WHERE asset.scope='project' AND asset.project_id=:project_id"""

    # Run snapshots were already deleted by purge_private_scope. Remove both
    # sides of a project Credential/MCP grant before any envelope or slot.
    await session.execute(
        text(
            f"""DELETE FROM credential_grants AS grant_row
                 WHERE grant_row.credential_version_id IN ({project_credential_versions})
                    OR grant_row.mcp_server_version_id IN ({project_mcp_versions})"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE credential_envelopes
                    SET rotated_from_envelope_id=NULL
                  WHERE rotated_from_envelope_id IN (
                      SELECT envelope.id
                      FROM credential_envelopes AS envelope
                      WHERE envelope.credential_version_id IN ({project_credential_versions})
                  )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM credential_envelopes
                 WHERE credential_version_id IN ({project_credential_versions})"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """UPDATE credentials
                  SET current_version_id=NULL, status='revoked', source_key=NULL,
                      revoked_at=:purged_at, updated_at=:purged_at,
                      version=version + 1
                WHERE scope='project' AND project_id=:project_id"""
        ),
        parameters,
    )

    # Remove immutable published children only after the project is locked,
    # pending deletion, and due. The database trigger independently enforces
    # that same eligibility before allowing each child DELETE.
    await session.execute(
        text(
            """DELETE FROM agent_version_skill_refs AS ref
               WHERE ref.agent_version_id IN (
                   SELECT version.id FROM agent_versions AS version
                   JOIN agents AS asset ON asset.id=version.agent_id
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               ) OR ref.skill_version_id IN (
                   SELECT version.id FROM skill_versions AS version
                   JOIN skills AS asset ON asset.id=version.skill_id
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM agent_version_mcp_refs AS ref
               WHERE ref.agent_version_id IN (
                   SELECT version.id FROM agent_versions AS version
                   JOIN agents AS asset ON asset.id=version.agent_id
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               ) OR ref.mcp_server_version_id IN (
                   SELECT version.id FROM mcp_server_versions AS version
                   JOIN mcp_servers AS asset ON asset.id=version.mcp_server_id
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM skill_version_files AS file
               WHERE file.skill_version_id IN (
                   SELECT version.id FROM skill_versions AS version
                   JOIN skills AS asset ON asset.id=version.skill_id
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM mcp_version_credential_slots AS slot
                 WHERE slot.mcp_server_version_id IN ({project_mcp_versions})"""
        ),
        parameters,
    )

    for table_name in ("agents", "skills", "mcp_servers"):
        await session.execute(
            text(
                f"""UPDATE {table_name}
                        SET current_published_version_id=NULL, status='archived',
                            source_key=NULL, updated_at=:purged_at,
                            version=version + 1
                      WHERE scope='project' AND project_id=:project_id"""
            ),
            parameters,
        )

    await _delete_project_version_leaves(
        session,
        project_id=project_uuid,
        asset_table="credentials",
        version_table="credential_versions",
        asset_id_column="credential_id",
    )
    await _delete_project_version_leaves(
        session,
        project_id=project_uuid,
        asset_table="agents",
        version_table="agent_versions",
        asset_id_column="agent_id",
    )
    await _delete_project_version_leaves(
        session,
        project_id=project_uuid,
        asset_table="skills",
        version_table="skill_versions",
        asset_id_column="skill_id",
    )
    await _delete_project_version_leaves(
        session,
        project_id=project_uuid,
        asset_table="mcp_servers",
        version_table="mcp_server_versions",
        asset_id_column="mcp_server_id",
    )

    await session.execute(
        text(
            """DELETE FROM credentials
               WHERE scope='project' AND project_id=:project_id"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM skills
               WHERE scope='project' AND project_id=:project_id"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM mcp_servers
               WHERE scope='project' AND project_id=:project_id"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM agents AS asset
               WHERE asset.scope='project' AND asset.project_id=:project_id
                 AND NOT EXISTS (
                     SELECT 1 FROM threads_meta AS thread
                     WHERE thread.agent_asset_id=asset.id
                       AND thread.agent_scope='project'
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM scheduled_tasks AS task
                     WHERE task.agent_asset_id=asset.id
                       AND task.agent_scope='project'
                 )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """UPDATE agents
                  SET slug='purged-' || replace(id::text, '-', ''),
                      display_name='purged', status='archived', source_key=NULL,
                      updated_at=:purged_at, version=version + 1
                WHERE scope='project' AND project_id=:project_id"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """UPDATE projects
                  SET display_name='Deleted project', description='', icon='folder',
                      updated_at=:purged_at
                WHERE id=:project_id"""
        ),
        parameters,
    )


class RetentionPurger:
    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        audit: TrustedOperationAuditSink,
        quota: ProjectQuotaEnforcer,
        repository: RetentionPurgeRepository | None = None,
    ) -> None:
        if type(audit) is not TrustedOperationAuditSink:
            raise TypeError("retention purge requires audit authority")
        if type(quota) is not ProjectQuotaEnforcer:
            raise TypeError("retention purge requires quota authority")
        self._sessions = sessions
        self._audit = audit
        self._quota = quota
        self.repository = repository or RetentionPurgeRepository()

    async def purge(
        self,
        candidate: RetentionCandidate,
        *,
        now: datetime | None = None,
    ) -> RetentionPurgeResult:
        if type(candidate) is not RetentionCandidate:
            raise TypeError("retention candidate is required")
        purged_at = _aware(now or datetime.now(UTC))
        purge_id = retention_purge_id(candidate.idempotency_key)
        async with self._sessions() as session, session.begin():
            await self.repository.verify_still_eligible(session, candidate, now=purged_at)
            purged_count = await self.repository.physically_purge(
                session,
                candidate,
                quota=self._quota,
            )
            await session.flush()
            await self._audit.purge_completed(
                session,
                purge_id=purge_id,
                project_id=(None if candidate.resource_kind == "account" else candidate.project_id),
                resource_kind=candidate.resource_kind,
                purged_count=purged_count,
                request_id=candidate.request_id,
            )
        return RetentionPurgeResult(
            purge_id=purge_id,
            resource_kind=candidate.resource_kind,
            purged_count=purged_count,
            purged_at=purged_at,
        )


__all__ = [
    "RetentionCandidate",
    "RetentionNotEligible",
    "RetentionPurgeResult",
    "RetentionPurgeRepository",
    "RetentionPurger",
    "purge_private_scope",
    "purge_project_channel_guest_scope",
    "retention_purge_id",
]
