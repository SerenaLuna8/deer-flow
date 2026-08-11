"""PostgreSQL Workflow HTTP effect ledger and Job-lease coordinator.

This Phase-0 adapter targets the single ``workflow_node_effects`` authority
reviewed by G05.  G10 owns the production migration.  Every state transition
locks and validates the current Workflow Run, epoch mapping and raw Job lease
in the same transaction as the effect mutation.  The raw lease exists only in
Worker memory; PostgreSQL stores its canonical Job hash.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.workflows.contracts import (
    WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER,
    WorkflowHttpSettledOutcomeV1,
)
from app.workflows.http_effects import (
    WorkflowHttpEffectIdentityV1,
    WorkflowHttpEffectRecordV1,
    WorkflowHttpJobExecutionFence,
    require_workflow_http_settled_outcome,
    workflow_http_settled_outcome_digest,
)


class WorkflowHttpEffectStoreError(RuntimeError):
    pass


class WorkflowHttpEffectNotFound(WorkflowHttpEffectStoreError):
    pass


class WorkflowHttpEffectConflict(WorkflowHttpEffectStoreError):
    pass


class WorkflowHttpEffectAlreadyDispatching(WorkflowHttpEffectStoreError):
    pass


class WorkflowHttpExecutionAuthorityLost(WorkflowHttpEffectStoreError):
    """The caller no longer owns the exact current Job/epoch/raw lease."""


class WorkflowHttpPreDispatchAuthorityDenied(WorkflowHttpEffectStoreError):
    """Live capability/Credential authority did not return the strict True grant."""


class WorkflowHttpSideEffectUnknown(WorkflowHttpEffectStoreError):
    """Terminal error: neither API nor UI may offer retry for the same Run."""


class WorkflowHttpSafeFailure(WorkflowHttpEffectStoreError):
    """Recovered deterministic pre-dispatch failure; never auto-redispatch."""

    def __init__(self, safe_error_code: str):
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code


class WorkflowHttpDispatchFailure(WorkflowHttpEffectStoreError):
    def __init__(self, safe_error_code: str, *, may_have_reached_origin: bool):
        if type(safe_error_code) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", safe_error_code) is None:
            raise ValueError("Workflow HTTP dispatch safe error code is invalid")
        if type(may_have_reached_origin) is not bool:
            raise ValueError("Workflow HTTP dispatch origin-reachability flag must be a real boolean")
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code
        self.may_have_reached_origin = may_have_reached_origin


def _require_fence(
    value: WorkflowHttpJobExecutionFence,
) -> WorkflowHttpJobExecutionFence:
    if type(value) is not WorkflowHttpJobExecutionFence:
        raise TypeError("fence must be WorkflowHttpJobExecutionFence")
    return value


def _dump_outcome(outcome: WorkflowHttpSettledOutcomeV1) -> str:
    outcome = require_workflow_http_settled_outcome(outcome)
    return json.dumps(
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.dump_python(outcome, mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class PostgresWorkflowHttpEffectStore:
    """Row-lock transitions fenced by the canonical Workflow Job authority."""

    def __init__(self, engine: AsyncEngine):
        if not isinstance(engine, AsyncEngine):
            raise TypeError("engine must be an AsyncEngine")
        self._engine = engine

    @staticmethod
    def _record(row: object) -> WorkflowHttpEffectRecordV1:
        mapping = row
        outcome = (
            None
            if mapping["outcome_json"] is None
            else WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_json(
                json.dumps(
                    mapping["outcome_json"],
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        )
        identity = WorkflowHttpEffectIdentityV1(
            schema_version=1,
            effect_id=uuid.UUID(str(mapping["id"])),
            run_id=uuid.UUID(str(mapping["workflow_run_id"])),
            workflow_version_id=uuid.UUID(str(mapping["workflow_version_id"])),
            node_id=uuid.UUID(str(mapping["node_id"])),
            activation_key=mapping["activation_key"],
            operation_key=mapping["operation_key"],
            method=mapping["http_method"],
            request_fingerprint=mapping["request_hmac"],
            idempotency_key=mapping["provider_idempotency_key"],
        )
        return WorkflowHttpEffectRecordV1(
            schema_version=1,
            identity=identity,
            state=mapping["status"],
            revision=mapping["revision"],
            dispatch_job_id=mapping["dispatch_job_id"],
            dispatch_execution_epoch=mapping["dispatch_execution_epoch"],
            dispatch_attempt=mapping["dispatch_attempt"],
            dispatch_owner=(None if mapping["dispatch_owner_id"] is None else str(mapping["dispatch_owner_id"])),
            dispatch_lease_token_hash=mapping["dispatch_lease_token_hash"],
            dispatch_started_at=mapping["dispatch_started_at"],
            outcome=outcome,
            outcome_digest=mapping["outcome_digest"],
            safe_error_code=mapping["safe_error_code"],
            updated_at=mapping["updated_at"],
        )

    async def _require_active_authority(
        self,
        connection: object,
        fence: WorkflowHttpJobExecutionFence,
    ) -> None:
        """Lock and validate complete current Run/Job/epoch/raw-lease authority."""

        result = await connection.execute(
            text(
                """
                SELECT j.id
                  FROM jobs AS j
                  JOIN workflow_runs AS r
                    ON r.id = j.workflow_run_id
                   AND r.project_id = j.project_id
                   AND r.owner_user_id = j.owner_user_id
                   AND r.origin_trace_id = j.origin_trace_id
                  JOIN workflow_run_jobs AS m
                    ON m.workflow_run_id = j.workflow_run_id
                   AND m.execution_epoch = j.workflow_epoch
                   AND m.job_id = j.id
                   AND m.project_id = j.project_id
                   AND m.owner_user_id = j.owner_user_id
                 WHERE j.id = :job_id
                   AND j.job_type = 'workflow_run'
                   AND j.project_id = :project_id
                   AND j.owner_user_id = :owner_user_id
                   AND j.workflow_run_id = :workflow_run_id
                   AND j.workflow_epoch = :execution_epoch
                   AND j.origin_trace_id = :origin_trace_id
                   AND j.attempt_count = :attempt
                   AND j.lease_owner_id = :worker_id
                   AND j.lease_token_hash = :lease_token_hash
                   AND j.status IN ('leased', 'running')
                   AND j.lease_expires_at > CURRENT_TIMESTAMP
                   AND j.cancel_requested_at IS NULL
                   AND r.status = 'running'
                   AND r.current_job_id = j.id
                   AND r.execution_epoch = j.workflow_epoch
                 FOR UPDATE OF j, r
                """
            ),
            {
                "job_id": fence.job_id,
                "project_id": fence.project_id,
                "owner_user_id": fence.owner_user_id,
                "workflow_run_id": fence.run_id,
                "execution_epoch": fence.execution_epoch,
                "origin_trace_id": fence.origin_trace_id,
                "attempt": fence.attempt,
                "worker_id": fence.worker_id,
                "lease_token_hash": fence.lease_token_hash,
            },
        )
        if result.one_or_none() is None:
            raise WorkflowHttpExecutionAuthorityLost("Workflow HTTP Job execution authority is no longer current")

    async def require_active_authority(
        self,
        fence: WorkflowHttpJobExecutionFence,
    ) -> None:
        """Revalidate immediately before entering the external dispatch port."""

        fence = _require_fence(fence)
        async with self._engine.begin() as connection:
            await self._require_active_authority(connection, fence)

    async def _locked(self, connection: object, effect_id: uuid.UUID):
        result = await connection.execute(
            text(
                """
                SELECT e.id, e.project_id, e.owner_user_id, e.workflow_run_id,
                       r.workflow_version_id, e.node_id, e.activation_key,
                       e.operation_key, e.http_method, e.request_hmac,
                       e.provider_idempotency_key, e.status, e.revision,
                       e.dispatch_job_id, e.dispatch_execution_epoch,
                       e.dispatch_attempt, e.dispatch_owner_id,
                       e.dispatch_lease_token_hash, e.dispatch_started_at,
                       e.outcome_json, e.outcome_digest, e.safe_error_code,
                       e.updated_at
                  FROM workflow_node_effects AS e
                  JOIN workflow_runs AS r
                    ON r.id = e.workflow_run_id
                   AND r.project_id = e.project_id
                   AND r.owner_user_id = e.owner_user_id
                 WHERE e.id = :effect_id
                   FOR UPDATE OF e
                """
            ),
            {"effect_id": effect_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise WorkflowHttpEffectNotFound("Workflow HTTP effect does not exist")
        return row

    async def get(self, effect_id: uuid.UUID) -> WorkflowHttpEffectRecordV1:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT e.id, e.project_id, e.owner_user_id,
                           e.workflow_run_id, r.workflow_version_id, e.node_id,
                           e.activation_key, e.operation_key, e.http_method,
                           e.request_hmac, e.provider_idempotency_key, e.status,
                           e.revision, e.dispatch_job_id,
                           e.dispatch_execution_epoch, e.dispatch_attempt,
                           e.dispatch_owner_id, e.dispatch_lease_token_hash,
                           e.dispatch_started_at, e.outcome_json,
                           e.outcome_digest, e.safe_error_code, e.updated_at
                      FROM workflow_node_effects AS e
                      JOIN workflow_runs AS r
                        ON r.id = e.workflow_run_id
                       AND r.project_id = e.project_id
                       AND r.owner_user_id = e.owner_user_id
                     WHERE e.id = :effect_id
                    """
                ),
                {"effect_id": effect_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise WorkflowHttpEffectNotFound("Workflow HTTP effect does not exist")
        return self._record(row)

    async def prepare(
        self,
        identity: WorkflowHttpEffectIdentityV1,
        *,
        fence: WorkflowHttpJobExecutionFence,
    ) -> WorkflowHttpEffectRecordV1:
        if type(identity) is not WorkflowHttpEffectIdentityV1:
            raise TypeError("identity must be WorkflowHttpEffectIdentityV1")
        fence = _require_fence(fence)
        if identity.method in {"GET", "HEAD"}:
            raise ValueError("workflow_node_effects is the write-effect authority")
        if identity.run_id != fence.run_id:
            raise WorkflowHttpEffectConflict("Workflow HTTP effect Run does not match its Job fence")
        async with self._engine.begin() as connection:
            await self._require_active_authority(connection, fence)
            await connection.execute(
                text(
                    """
                    INSERT INTO workflow_node_effects (
                        id, project_id, owner_user_id, workflow_run_id, node_id,
                        activation_key, operation_key, http_method, status,
                        request_hmac, provider_idempotency_key, revision
                    ) VALUES (
                        :effect_id, :project_id, :owner_user_id, :run_id,
                        :node_id, :activation_key, :operation_key, :method,
                        'prepared', :request_fingerprint, :idempotency_key, 1
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    **identity.model_dump(mode="python"),
                    "project_id": fence.project_id,
                    "owner_user_id": fence.owner_user_id,
                },
            )
            result = await connection.execute(
                text(
                    """
                    SELECT e.id, e.project_id, e.owner_user_id,
                           e.workflow_run_id, r.workflow_version_id, e.node_id,
                           e.activation_key, e.operation_key, e.http_method,
                           e.request_hmac, e.provider_idempotency_key, e.status,
                           e.revision, e.dispatch_job_id,
                           e.dispatch_execution_epoch, e.dispatch_attempt,
                           e.dispatch_owner_id, e.dispatch_lease_token_hash,
                           e.dispatch_started_at, e.outcome_json,
                           e.outcome_digest, e.safe_error_code, e.updated_at
                      FROM workflow_node_effects AS e
                      JOIN workflow_runs AS r
                        ON r.id = e.workflow_run_id
                       AND r.project_id = e.project_id
                       AND r.owner_user_id = e.owner_user_id
                     WHERE e.workflow_run_id = :run_id
                       AND e.node_id = :node_id
                       AND e.activation_key = :activation_key
                       AND e.operation_key = :operation_key
                       AND e.project_id = :project_id
                       AND e.owner_user_id = :owner_user_id
                       FOR UPDATE OF e
                    """
                ),
                {
                    "run_id": identity.run_id,
                    "node_id": identity.node_id,
                    "activation_key": identity.activation_key,
                    "operation_key": identity.operation_key,
                    "project_id": fence.project_id,
                    "owner_user_id": fence.owner_user_id,
                },
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise WorkflowHttpEffectConflict("Workflow HTTP activation already has a different operation")
            record = self._record(row)
            if record.identity.model_dump(mode="python", exclude={"effect_id"}) != identity.model_dump(mode="python", exclude={"effect_id"}):
                raise WorkflowHttpEffectConflict("Workflow HTTP activation already has a different effect identity")
            return record

    @staticmethod
    def _require_dispatch_fence(
        record: WorkflowHttpEffectRecordV1,
        fence: WorkflowHttpJobExecutionFence,
    ) -> None:
        if not (
            record.state == "dispatching"
            and record.identity.run_id == fence.run_id
            and record.dispatch_job_id == fence.job_id
            and record.dispatch_execution_epoch == fence.execution_epoch
            and record.dispatch_attempt == fence.attempt
            and record.dispatch_owner == str(fence.worker_id)
            and record.dispatch_lease_token_hash == fence.lease_token_hash
        ):
            raise WorkflowHttpEffectConflict("Workflow HTTP transition does not own the exact dispatch fence")

    async def claim_for_dispatch(
        self,
        effect_id: uuid.UUID,
        *,
        fence: WorkflowHttpJobExecutionFence,
    ) -> WorkflowHttpEffectRecordV1:
        fence = _require_fence(fence)
        async with self._engine.begin() as connection:
            await self._require_active_authority(connection, fence)
            record = self._record(await self._locked(connection, effect_id))
            if record.identity.run_id != fence.run_id:
                raise WorkflowHttpEffectConflict("Workflow HTTP effect Run changed")
            if record.state == "settled":
                return record
            if record.state == "unknown":
                raise WorkflowHttpSideEffectUnknown("Workflow HTTP write outcome is unknown and cannot be retried")
            if record.state == "failed_safe":
                assert record.safe_error_code is not None
                raise WorkflowHttpSafeFailure(record.safe_error_code)
            if record.state == "dispatching":
                raise WorkflowHttpEffectAlreadyDispatching("Workflow HTTP effect is already dispatching")
            await connection.execute(
                text(
                    """
                    UPDATE workflow_node_effects
                       SET status = 'dispatching',
                           revision = revision + 1,
                           dispatch_job_id = :job_id,
                           dispatch_execution_epoch = :execution_epoch,
                           dispatch_attempt = :attempt,
                           dispatch_owner_id = :worker_id,
                           dispatch_lease_token_hash = :lease_token_hash,
                           dispatch_started_at = CURRENT_TIMESTAMP,
                           safe_error_code = NULL,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = :effect_id
                    """
                ),
                {
                    "effect_id": effect_id,
                    "job_id": fence.job_id,
                    "execution_epoch": fence.execution_epoch,
                    "attempt": fence.attempt,
                    "worker_id": fence.worker_id,
                    "lease_token_hash": fence.lease_token_hash,
                },
            )
            return self._record(await self._locked(connection, effect_id))

    async def settle(
        self,
        effect_id: uuid.UUID,
        *,
        fence: WorkflowHttpJobExecutionFence,
        outcome: WorkflowHttpSettledOutcomeV1,
    ) -> WorkflowHttpEffectRecordV1:
        fence = _require_fence(fence)
        outcome = require_workflow_http_settled_outcome(outcome)
        digest = workflow_http_settled_outcome_digest(outcome)
        async with self._engine.begin() as connection:
            await self._require_active_authority(connection, fence)
            record = self._record(await self._locked(connection, effect_id))
            if record.state == "settled":
                if record.outcome_digest != digest:
                    raise WorkflowHttpEffectConflict("settled Workflow HTTP effect has a different outcome")
                return record
            self._require_dispatch_fence(record, fence)
            await connection.execute(
                text(
                    """
                    UPDATE workflow_node_effects
                       SET status = 'settled',
                           revision = revision + 1,
                           dispatch_owner_id = NULL,
                           dispatch_lease_token_hash = NULL,
                           outcome_json = CAST(:outcome AS jsonb),
                           outcome_digest = :outcome_digest,
                           safe_error_code = NULL,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = :effect_id
                    """
                ),
                {
                    "effect_id": effect_id,
                    "outcome": _dump_outcome(outcome),
                    "outcome_digest": digest,
                },
            )
            return self._record(await self._locked(connection, effect_id))

    async def fail_safe(
        self,
        effect_id: uuid.UUID,
        *,
        fence: WorkflowHttpJobExecutionFence,
        safe_error_code: str,
    ) -> WorkflowHttpEffectRecordV1:
        fence = _require_fence(fence)
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", safe_error_code) is None:
            raise ValueError("safe_error_code is invalid")
        async with self._engine.begin() as connection:
            await self._require_active_authority(connection, fence)
            record = self._record(await self._locked(connection, effect_id))
            self._require_dispatch_fence(record, fence)
            await connection.execute(
                text(
                    """
                    UPDATE workflow_node_effects
                       SET status = 'failed_safe',
                           revision = revision + 1,
                           dispatch_owner_id = NULL,
                           dispatch_lease_token_hash = NULL,
                           safe_error_code = :safe_error_code,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = :effect_id
                    """
                ),
                {
                    "effect_id": effect_id,
                    "safe_error_code": safe_error_code,
                },
            )
            return self._record(await self._locked(connection, effect_id))

    async def mark_unknown(
        self,
        effect_id: uuid.UUID,
        *,
        fence: WorkflowHttpJobExecutionFence,
    ) -> WorkflowHttpEffectRecordV1:
        fence = _require_fence(fence)
        async with self._engine.begin() as connection:
            await self._require_active_authority(connection, fence)
            record = self._record(await self._locked(connection, effect_id))
            self._require_dispatch_fence(record, fence)
            await connection.execute(
                text(
                    """
                    UPDATE workflow_node_effects
                       SET status = 'unknown',
                           revision = revision + 1,
                           dispatch_owner_id = NULL,
                           dispatch_lease_token_hash = NULL,
                           safe_error_code = 'SIDE_EFFECT_STATE_UNKNOWN',
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = :effect_id
                    """
                ),
                {"effect_id": effect_id},
            )
            return self._record(await self._locked(connection, effect_id))

    async def recover_abandoned_dispatch(
        self,
        effect_id: uuid.UUID,
        *,
        recovery_fence: WorkflowHttpJobExecutionFence,
    ) -> WorkflowHttpEffectRecordV1:
        """Let only a new current Job attempt close an abandoned write fence."""

        recovery_fence = _require_fence(recovery_fence)
        async with self._engine.begin() as connection:
            await self._require_active_authority(connection, recovery_fence)
            record = self._record(await self._locked(connection, effect_id))
            if record.identity.run_id != recovery_fence.run_id:
                raise WorkflowHttpEffectConflict("Workflow HTTP effect Run changed")
            if record.state != "dispatching":
                return record
            same_fence = (
                record.dispatch_job_id == recovery_fence.job_id
                and record.dispatch_execution_epoch == recovery_fence.execution_epoch
                and record.dispatch_attempt == recovery_fence.attempt
                and record.dispatch_owner == str(recovery_fence.worker_id)
                and record.dispatch_lease_token_hash == recovery_fence.lease_token_hash
            )
            if same_fence:
                raise WorkflowHttpEffectConflict("the current dispatch owner cannot recover its own live fence")
            await connection.execute(
                text(
                    """
                    UPDATE workflow_node_effects
                       SET status = 'unknown',
                           revision = revision + 1,
                           dispatch_owner_id = NULL,
                           dispatch_lease_token_hash = NULL,
                           safe_error_code = 'SIDE_EFFECT_STATE_UNKNOWN',
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = :effect_id
                    """
                ),
                {"effect_id": effect_id},
            )
            return self._record(await self._locked(connection, effect_id))


class WorkflowHttpEffectExecutor:
    """Settle-before-checkpoint coordinator used by the Workflow HTTP node."""

    def __init__(self, store: PostgresWorkflowHttpEffectStore):
        if type(store) is not PostgresWorkflowHttpEffectStore:
            raise TypeError("store must be PostgresWorkflowHttpEffectStore")
        self._store = store

    async def execute(
        self,
        identity: WorkflowHttpEffectIdentityV1,
        *,
        fence: WorkflowHttpJobExecutionFence,
        authorize_dispatch: Callable[[], Awaitable[bool]],
        dispatch: Callable[[str | None], Awaitable[WorkflowHttpSettledOutcomeV1]],
        after_settle_before_checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> WorkflowHttpSettledOutcomeV1:
        record = await self._store.prepare(identity, fence=fence)
        if record.state == "settled":
            assert record.outcome is not None
            return record.outcome
        if record.state == "unknown":
            raise WorkflowHttpSideEffectUnknown("Workflow HTTP write outcome is unknown and cannot be retried")
        if record.state == "failed_safe":
            assert record.safe_error_code is not None
            raise WorkflowHttpSafeFailure(record.safe_error_code)
        effect_id = record.identity.effect_id
        # G44 supplies this required port with the live membership/capability
        # and exact Run Credential snapshot/grant/revoke/project/schema checks.
        # Phase 0 deliberately has no ambient/config fallback.
        await self._store.require_active_authority(fence)
        try:
            authorized = await authorize_dispatch()
        except Exception:
            raise WorkflowHttpPreDispatchAuthorityDenied("Workflow HTTP live dispatch authority failed closed") from None
        if type(authorized) is not bool or authorized is not True:
            raise WorkflowHttpPreDispatchAuthorityDenied("Workflow HTTP live dispatch authority must return strict True")
        # Revalidate after the potentially remote authority closure, then make
        # the atomic prepared->dispatching claim the final operation before the
        # external port. A crash or takeover during authority evaluation leaves
        # the effect prepared and therefore never creates a false unknown.
        await self._store.require_active_authority(fence)
        await self._store.claim_for_dispatch(effect_id, fence=fence)
        try:
            outcome = await dispatch(identity.idempotency_key)
        except WorkflowHttpDispatchFailure as error:
            if error.may_have_reached_origin:
                await self._store.mark_unknown(effect_id, fence=fence)
                raise WorkflowHttpSideEffectUnknown("Workflow HTTP write outcome is unknown and cannot be retried") from None
            await self._store.fail_safe(
                effect_id,
                fence=fence,
                safe_error_code=error.safe_error_code,
            )
            raise
        settled = await self._store.settle(
            effect_id,
            fence=fence,
            outcome=outcome,
        )
        if after_settle_before_checkpoint is not None:
            await after_settle_before_checkpoint()
        assert settled.outcome is not None
        return settled.outcome


__all__ = [
    "PostgresWorkflowHttpEffectStore",
    "WorkflowHttpDispatchFailure",
    "WorkflowHttpEffectAlreadyDispatching",
    "WorkflowHttpEffectConflict",
    "WorkflowHttpEffectExecutor",
    "WorkflowHttpEffectNotFound",
    "WorkflowHttpExecutionAuthorityLost",
    "WorkflowHttpPreDispatchAuthorityDenied",
    "WorkflowHttpSafeFailure",
    "WorkflowHttpSideEffectUnknown",
]
