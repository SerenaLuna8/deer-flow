"""Fail-closed live and admitted-snapshot runtime-policy materialization."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.system_runtime_settings.errors import SystemRuntimePolicyUnavailable
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedWorkflowRuntimePolicy,
    RuntimePolicySection,
    RuntimePolicyValue,
)
from app.system_runtime_settings.repository import (
    SystemRuntimePolicyRepository,
    SystemRuntimePolicyRepositoryInvariant,
)
from app.system_runtime_settings.validation import RuntimePolicyInvalid, canonical_policy_payload, parse_policy_value, runtime_policy_schema_version
from app.workflows.runtime_policy import WorkflowRuntimePolicyV1
from deerflow.persistence.system_runtime_settings import SystemRuntimePolicyVersionRow
from deerflow.persistence.workflows import WorkflowRunRuntimePolicySnapshotRow


def _materialize_exact(
    section: RuntimePolicySection,
    *,
    schema_version: int,
    value: dict[str, object],
    checksum: str,
) -> RuntimePolicyValue:
    if schema_version != runtime_policy_schema_version(section):
        raise SystemRuntimePolicyRepositoryInvariant
    canonical = canonical_policy_payload(section, value)
    if canonical.schema_version != schema_version or canonical.checksum != checksum:
        raise SystemRuntimePolicyRepositoryInvariant
    return parse_policy_value(section, canonical.value)


class SystemRuntimePolicyMaterializer:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    async def materialize_current_with_revision_in_session(
        session: AsyncSession,
        section: RuntimePolicySection | str,
        *,
        for_update: bool = False,
    ) -> tuple[RuntimePolicyValue, int]:
        """Materialize one locked current pointer and return its exact revision.

        Callers that must freeze both the policy value and its revision must not
        issue a second ``current`` query: the pointer could otherwise change
        between reads.  The repository join supplies both from one snapshot.
        """

        try:
            parsed_section = RuntimePolicySection(section)
            _policy, version = await SystemRuntimePolicyRepository(session).current(
                parsed_section,
                for_update=for_update,
            )
            return (
                _materialize_exact(
                    parsed_section,
                    schema_version=int(version.schema_version),
                    value=dict(version.value),
                    checksum=version.payload_checksum,
                ),
                int(version.version_number),
            )
        except SystemRuntimePolicyUnavailable:
            raise
        except (
            DBAPIError,
            RuntimeError,
            RuntimePolicyInvalid,
            SystemRuntimePolicyRepositoryInvariant,
            TypeError,
            ValueError,
        ):
            raise SystemRuntimePolicyUnavailable from None

    @staticmethod
    async def materialize_current_in_session(
        session: AsyncSession,
        section: RuntimePolicySection | str,
        *,
        for_update: bool = False,
    ) -> RuntimePolicyValue:
        value, _revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
            session,
            section,
            for_update=for_update,
        )
        return value

    async def materialize_current(
        self,
        section: RuntimePolicySection | str,
    ) -> RuntimePolicyValue:
        try:
            async with self._session_factory() as session, session.begin():
                return await self.materialize_current_in_session(session, section)
        except SystemRuntimePolicyUnavailable:
            raise
        except (DBAPIError, RuntimeError):
            raise SystemRuntimePolicyUnavailable from None

    @staticmethod
    def _locked_workflow_runtime(
        version: SystemRuntimePolicyVersionRow,
        *,
        revision: int,
        value_override: dict[str, object] | None = None,
    ) -> LockedWorkflowRuntimePolicy:
        value_payload = dict(version.value) if value_override is None else value_override
        value = _materialize_exact(
            RuntimePolicySection.WORKFLOW_RUNTIME,
            schema_version=int(version.schema_version),
            value=value_payload,
            checksum=version.payload_checksum,
        )
        if type(value) is not WorkflowRuntimePolicyV1 or int(version.version_number) != revision or value.model_dump(mode="json") != dict(version.value):
            raise SystemRuntimePolicyRepositoryInvariant
        return LockedWorkflowRuntimePolicy.create(
            policy_version_id=uuid.UUID(str(version.id)),
            revision=revision,
            schema_version=int(version.schema_version),
            payload_checksum=version.payload_checksum,
            value=value,
        )

    @staticmethod
    async def materialize_workflow_runtime_current_locked_in_session(
        session: AsyncSession,
        *,
        for_update: bool = False,
    ) -> LockedWorkflowRuntimePolicy:
        try:
            policy, version = await SystemRuntimePolicyRepository(session).current(
                RuntimePolicySection.WORKFLOW_RUNTIME,
                for_update=for_update,
            )
            if policy.current_version_id != version.id:
                raise SystemRuntimePolicyRepositoryInvariant
            return SystemRuntimePolicyMaterializer._locked_workflow_runtime(
                version,
                revision=int(policy.revision),
            )
        except SystemRuntimePolicyUnavailable:
            raise
        except (
            DBAPIError,
            RuntimeError,
            RuntimePolicyInvalid,
            SystemRuntimePolicyRepositoryInvariant,
            TypeError,
            ValueError,
        ):
            raise SystemRuntimePolicyUnavailable from None

    async def materialize_workflow_runtime_current_locked(
        self,
    ) -> LockedWorkflowRuntimePolicy:
        try:
            async with self._session_factory() as session, session.begin():
                return await self.materialize_workflow_runtime_current_locked_in_session(
                    session,
                )
        except SystemRuntimePolicyUnavailable:
            raise
        except (DBAPIError, RuntimeError):
            raise SystemRuntimePolicyUnavailable from None

    @staticmethod
    async def materialize_workflow_runtime_current_in_session(
        session: AsyncSession,
        *,
        for_update: bool = False,
    ) -> WorkflowRuntimePolicyV1:
        return (
            await SystemRuntimePolicyMaterializer.materialize_workflow_runtime_current_locked_in_session(
                session,
                for_update=for_update,
            )
        ).value

    async def materialize_workflow_runtime_current(
        self,
    ) -> WorkflowRuntimePolicyV1:
        try:
            async with self._session_factory() as session, session.begin():
                return await self.materialize_workflow_runtime_current_in_session(
                    session,
                )
        except SystemRuntimePolicyUnavailable:
            raise
        except (DBAPIError, RuntimeError):
            raise SystemRuntimePolicyUnavailable from None

    @staticmethod
    async def materialize_workflow_run_snapshot_in_session(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        workflow_run_id: uuid.UUID,
    ) -> LockedWorkflowRuntimePolicy:
        try:
            if not isinstance(session, AsyncSession) or not session.in_transaction() or not isinstance(project_id, uuid.UUID) or not isinstance(workflow_run_id, uuid.UUID) or type(owner_user_id) is not str or not owner_user_id:
                raise SystemRuntimePolicyRepositoryInvariant
            result = (
                await session.execute(
                    select(
                        WorkflowRunRuntimePolicySnapshotRow,
                        SystemRuntimePolicyVersionRow,
                    )
                    .join(
                        SystemRuntimePolicyVersionRow,
                        (SystemRuntimePolicyVersionRow.section == WorkflowRunRuntimePolicySnapshotRow.section)
                        & (SystemRuntimePolicyVersionRow.id == WorkflowRunRuntimePolicySnapshotRow.policy_version_id)
                        & (SystemRuntimePolicyVersionRow.version_number == WorkflowRunRuntimePolicySnapshotRow.revision)
                        & (SystemRuntimePolicyVersionRow.schema_version == WorkflowRunRuntimePolicySnapshotRow.schema_version)
                        & (SystemRuntimePolicyVersionRow.payload_checksum == WorkflowRunRuntimePolicySnapshotRow.payload_checksum),
                    )
                    .where(
                        WorkflowRunRuntimePolicySnapshotRow.workflow_run_id == workflow_run_id,
                        WorkflowRunRuntimePolicySnapshotRow.project_id == project_id,
                        WorkflowRunRuntimePolicySnapshotRow.owner_user_id == owner_user_id,
                        WorkflowRunRuntimePolicySnapshotRow.section == RuntimePolicySection.WORKFLOW_RUNTIME.value,
                    )
                    .with_for_update(
                        read=True,
                        of=(
                            WorkflowRunRuntimePolicySnapshotRow,
                            SystemRuntimePolicyVersionRow,
                        ),
                    )
                )
            ).one_or_none()
            if result is None:
                raise SystemRuntimePolicyRepositoryInvariant
            snapshot, version = result
            return SystemRuntimePolicyMaterializer._locked_workflow_runtime(
                version,
                revision=int(snapshot.revision),
                value_override=dict(snapshot.value_json),
            )
        except SystemRuntimePolicyUnavailable:
            raise
        except (
            DBAPIError,
            RuntimeError,
            RuntimePolicyInvalid,
            SystemRuntimePolicyRepositoryInvariant,
            TypeError,
            ValueError,
        ):
            raise SystemRuntimePolicyUnavailable from None

    async def materialize_workflow_run_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        workflow_run_id: uuid.UUID,
    ) -> LockedWorkflowRuntimePolicy:
        try:
            async with self._session_factory() as session, session.begin():
                return await self.materialize_workflow_run_snapshot_in_session(
                    session,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    workflow_run_id=workflow_run_id,
                )
        except SystemRuntimePolicyUnavailable:
            raise
        except (DBAPIError, RuntimeError):
            raise SystemRuntimePolicyUnavailable from None

    @staticmethod
    async def materialize_revision_in_session(
        session: AsyncSession,
        section: RuntimePolicySection | str,
        revision: int,
    ) -> RuntimePolicyValue:
        try:
            parsed_section = RuntimePolicySection(section)
            version = await SystemRuntimePolicyRepository(session).exact_version(
                parsed_section,
                revision,
            )
            if version is None or int(version.version_number) != revision:
                raise SystemRuntimePolicyRepositoryInvariant
            return _materialize_exact(
                parsed_section,
                schema_version=int(version.schema_version),
                value=dict(version.value),
                checksum=version.payload_checksum,
            )
        except SystemRuntimePolicyUnavailable:
            raise
        except (
            DBAPIError,
            RuntimeError,
            RuntimePolicyInvalid,
            SystemRuntimePolicyRepositoryInvariant,
            TypeError,
            ValueError,
        ):
            raise SystemRuntimePolicyUnavailable from None

    async def materialize_revision(
        self,
        section: RuntimePolicySection | str,
        revision: int,
    ) -> RuntimePolicyValue:
        try:
            async with self._session_factory() as session, session.begin():
                return await self.materialize_revision_in_session(
                    session,
                    section,
                    revision,
                )
        except SystemRuntimePolicyUnavailable:
            raise
        except (DBAPIError, RuntimeError):
            raise SystemRuntimePolicyUnavailable from None

    @staticmethod
    async def materialize_run_snapshot_in_session(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
    ) -> AgentRuntimePolicyValue:
        try:
            material = await SystemRuntimePolicyRepository(session).snapshot_material(
                project_id=project_id,
                owner_user_id=owner_user_id,
                run_id=run_id,
                section=RuntimePolicySection.AGENT_RUNTIME,
            )
            if material is None:
                raise SystemRuntimePolicyRepositoryInvariant
            snapshot, version = material
            value = _materialize_exact(
                RuntimePolicySection.AGENT_RUNTIME,
                schema_version=int(snapshot.schema_version),
                value=dict(version.value),
                checksum=snapshot.payload_checksum,
            )
            if not isinstance(value, AgentRuntimePolicyValue):
                raise SystemRuntimePolicyRepositoryInvariant
            return value
        except SystemRuntimePolicyUnavailable:
            raise
        except (
            DBAPIError,
            RuntimeError,
            RuntimePolicyInvalid,
            SystemRuntimePolicyRepositoryInvariant,
            TypeError,
            ValueError,
        ):
            raise SystemRuntimePolicyUnavailable from None

    async def materialize_run_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
    ) -> AgentRuntimePolicyValue:
        try:
            async with self._session_factory() as session, session.begin():
                return await self.materialize_run_snapshot_in_session(
                    session,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    run_id=run_id,
                )
        except SystemRuntimePolicyUnavailable:
            raise
        except (DBAPIError, RuntimeError):
            raise SystemRuntimePolicyUnavailable from None


__all__ = ["SystemRuntimePolicyMaterializer"]
