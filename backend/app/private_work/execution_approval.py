"""Compatibility façade for Local Provider host process execution approval.

Ownership lives in ``execution_approval_policy`` (frozen provider policy),
``execution_approval_codec`` (private envelope and receipt codec),
``execution_approval_lifecycle`` (shared transactional convergence and clocks),
``execution_approval_recovery`` (settlement and replay recovery),
``execution_approval_worker`` (lease-bound Worker port), and
``execution_approval_service`` (Gateway reads and decisions).  This module only
re-exports those same objects for existing import paths.
"""

from app.private_work.execution_approval_codec import (
    _PRIVATE_ENVELOPE_SCHEMA_VERSION as _PRIVATE_ENVELOPE_SCHEMA_VERSION,
)
from app.private_work.execution_approval_codec import (
    _RESULT_SCHEMA_VERSION as _RESULT_SCHEMA_VERSION,
)
from app.private_work.execution_approval_codec import (
    _RESULT_TEXT_LIMIT as _RESULT_TEXT_LIMIT,
)
from app.private_work.execution_approval_codec import (
    _bounded_text as _bounded_text,
)
from app.private_work.execution_approval_codec import (
    _frozen_plan_from_row as _frozen_plan_from_row,
)
from app.private_work.execution_approval_codec import (
    _outcome_from_receipt as _outcome_from_receipt,
)
from app.private_work.execution_approval_codec import (
    _private_envelope as _private_envelope,
)
from app.private_work.execution_approval_codec import (
    _result_payload as _result_payload,
)
from app.private_work.execution_approval_lifecycle import (
    _database_now as _database_now,
)
from app.private_work.execution_approval_lifecycle import (
    _now as _now,
)
from app.private_work.execution_approval_policy import (
    _HOST_EXECUTION_MODES as _HOST_EXECUTION_MODES,
)
from app.private_work.execution_approval_policy import (
    _PROVIDER_POLICY_SCHEMA_VERSION as _PROVIDER_POLICY_SCHEMA_VERSION,
)
from app.private_work.execution_approval_policy import (
    HostExecutionProviderPolicySnapshot as HostExecutionProviderPolicySnapshot,
)
from app.private_work.execution_approval_policy import (
    _canonical_digest as _canonical_digest,
)
from app.private_work.execution_approval_recovery import (
    _staged_approval_source_job_id as _staged_approval_source_job_id,
)
from app.private_work.execution_approval_recovery import (
    recover_staged_execution_approval_id as recover_staged_execution_approval_id,
)
from app.private_work.execution_approval_recovery import (
    settle_staged_execution_approvals as settle_staged_execution_approvals,
)
from app.private_work.execution_approval_service import (
    _CLAIM_TTL_SECONDS as _CLAIM_TTL_SECONDS,
)
from app.private_work.execution_approval_service import (
    _CONTINUATION_NAME as _CONTINUATION_NAME,
)
from app.private_work.execution_approval_service import (
    ExecutionApprovalProjection as ExecutionApprovalProjection,
)
from app.private_work.execution_approval_service import (
    ExecutionApprovalService as ExecutionApprovalService,
)
from app.private_work.execution_approval_service import (
    _decision_digest as _decision_digest,
)
from app.private_work.execution_approval_service import (
    _idempotency_digest as _idempotency_digest,
)
from app.private_work.execution_approval_worker import (
    WorkerHostExecutionApprovalPort as WorkerHostExecutionApprovalPort,
)
from app.private_work.execution_approval_worker import (
    _asset_closure as _asset_closure,
)

__all__ = [
    "ExecutionApprovalProjection",
    "ExecutionApprovalService",
    "HostExecutionProviderPolicySnapshot",
    "WorkerHostExecutionApprovalPort",
    "recover_staged_execution_approval_id",
    "settle_staged_execution_approvals",
]
