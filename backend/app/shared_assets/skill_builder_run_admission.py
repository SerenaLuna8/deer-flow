from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkRunQuotaExceeded,
)
from app.private_work.execution_profile import (
    RequestedRunExecutionProfile,
    RunExecutionProfileUnsupported,
    RunModelSelectionLocked,
    RunSelectedModelUnavailable,
)
from app.private_work.run_admission import (
    PrivateRunAdmissionAuditPort,
    PrivateRunAdmissionQuotaPort,
)
from app.private_work.run_repository import (
    PrivateRunConflict,
    PrivateRunCreate,
    PrivateRunRepository,
)
from app.private_work.snapshot_repository import (
    RunModelSnapshotAdmissionPort,
    RunRuntimePolicyAdmissionPort,
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.reliability.jobs import (
    JobIdempotencyConflict,
    JobScope,
    PrivateRunJobRepository,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetRunQuotaExceeded,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.shared_assets.skill_design_generation import (
    SkillBuilderDependencySnapshot,
    SkillDesignGenerationRequest,
)
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)
from deerflow.trace_context import normalize_trace_id


@dataclass(frozen=True, slots=True)
class SkillBuilderRunAdmission:
    run_id: str
    status: str
    thread_id: str


class _NoopQuota:
    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: object,
    ) -> None:
        del session, context, run


class _NoopAudit:
    async def run_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: object,
        job: object,
    ) -> None:
        del session, context, run, job


class SkillBuilderRunAdmissionService:
    """Admit a dedicated Builder Run inside the caller-owned turn transaction."""

    def __init__(
        self,
        session_factory,
        *,
        model_catalog: RunModelSnapshotAdmissionPort | None = None,
        runtime_policy: RunRuntimePolicyAdmissionPort | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        quota: PrivateRunAdmissionQuotaPort | None = None,
        audit: PrivateRunAdmissionAuditPort | None = None,
    ) -> None:
        self._resolver = ProjectAssetResolver(session_factory)
        self._snapshots = RunSnapshotRepository(
            session_factory,
            model_catalog=model_catalog,
            runtime_policy=runtime_policy,
            endpoint_policy=endpoint_policy,
        )
        self._quota = quota or _NoopQuota()
        self._audit = audit or _NoopAudit()

    async def admit_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        design: SkillDesignSessionRow,
        operation: SkillDesignOperationRow,
        request: SkillDesignGenerationRequest,
        *,
        turn_message: str,
        model_name: str | None,
        reasoning_effort: str | None,
    ) -> SkillBuilderRunAdmission:
        if not session.in_transaction():
            raise AssetConflict(context.request_id)
        context.require(Capability.SHARED_ASSETS_READ)
        context.require(Capability.SHARED_ASSETS_EDIT)
        if (
            design.project_id != context.project_id
            or design.owner_user_id != str(context.user_id)
            or operation.project_id != context.project_id
            or operation.owner_user_id != str(context.user_id)
            or operation.session_id != design.id
            or operation.operation_kind != "turn"
            or operation.status != "in_progress"
            or operation.run_id is not None
        ):
            raise AssetConflict(context.request_id)

        resolved = await self._resolver.resolve_internal_skill_builder_closure_in_session(
            session,
            context,
        )
        if (
            len(resolved.skills) != 1
            or resolved.skills[0].asset_id != design.skill_creator_skill_id
            or resolved.skills[0].version_id != design.skill_creator_version_id
            or resolved.skills[0].checksum != design.skill_creator_payload_checksum
        ):
            raise AssetConflict(context.request_id)

        private_context = PrivateWorkContext.from_project(context)
        threads = PrivateThreadRepository(session)
        thread_id = str(design.thread_id)
        thread = await threads.get(
            scope=private_context.resource_scope,
            thread_id=thread_id,
            lock=True,
            thread_kind="skill_builder",
        )
        first_turn = thread is None
        if first_turn:
            thread = await threads.create(
                scope=private_context.resource_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    asset_id=resolved.lead_agent.asset_id,
                    scope=resolved.lead_agent.scope.value,
                ),
                display_name=f"Skill Builder: {design.display_name}",
                metadata={"skill_design_session_id": str(design.id)},
                thread_kind="skill_builder",
            )
        if thread.agent_asset_id != resolved.lead_agent.asset_id or thread.agent_scope != resolved.lead_agent.scope.value:
            raise AssetConflict(context.request_id)

        thinking_enabled = None if reasoning_effort is None else reasoning_effort != "none"
        try:
            execution_profile = RequestedRunExecutionProfile(
                model_name=model_name,
                thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort,
            )
        except TypeError:
            raise AssetValidationFailed(context.request_id) from None
        origin_trace_id = normalize_trace_id(context.request_id)
        if origin_trace_id is None:
            raise AssetValidationFailed(context.request_id)
        try:
            prior_dependencies = (
                SkillBuilderDependencySnapshot.model_validate(
                    design.authoring_dependencies_json,
                )
                if design.authoring_dependencies_json is not None
                else None
            )
        except ValidationError:
            raise AssetConflict(context.request_id) from None
        if prior_dependencies is not None and prior_dependencies.draft_checksum != design.draft_checksum:
            raise AssetConflict(context.request_id)
        payload = self._run_input_payload(
            request,
            turn_message=turn_message,
            first_turn=first_turn,
            draft_checksum=design.draft_checksum,
            prior_dependency_references=(tuple(item.reference for item in prior_dependencies.requirements) if prior_dependencies is not None else ()),
            request_id=context.request_id,
        )
        run_request = PrivateRunCreate(
            metadata={},
            kwargs={
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": json.dumps(
                                payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    ]
                },
                "config": {},
                "stream_mode": ["values", "updates", "messages"],
                "stream_subgraphs": False,
            },
            origin_trace_id=origin_trace_id,
            execution_profile=execution_profile,
        )
        try:
            run = await self._snapshots.create_run_with_snapshot_in_session(
                session,
                private_context,
                thread_id,
                run_request,
                resolved,
                runtime_kind="skill_builder",
                admit_memory=False,
            )
            jobs = PrivateRunJobRepository(session)
            job = await jobs.enqueue(
                scope=JobScope(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                ),
                run_id=run.run_id,
                origin_trace_id=run.origin_trace_id,
            )
            run = await PrivateRunRepository(session).attach_job(
                scope=private_context.resource_scope,
                run_id=run.run_id,
                job_id=job.job_id,
            )
            operation.run_id = run.run_id
            await self._quota.reserve_concurrent_run(session, private_context, run)
            await self._audit.run_admitted(session, private_context, run, job)
        except PrivateWorkRunQuotaExceeded:
            raise AssetRunQuotaExceeded(context.request_id) from None
        except (
            JobIdempotencyConflict,
            PrivateRunConflict,
            RunExecutionProfileUnsupported,
            RunModelSelectionLocked,
            RunSelectedModelUnavailable,
            RunSnapshotAssetStale,
        ):
            raise AssetConflict(context.request_id) from None
        except PrivateWorkError:
            raise AssetStorageUnavailable(context.request_id) from None
        await session.flush()
        return SkillBuilderRunAdmission(
            run_id=run.run_id,
            status=run.status,
            thread_id=run.thread_id,
        )

    @staticmethod
    def _run_input_payload(
        request: SkillDesignGenerationRequest,
        *,
        turn_message: str,
        first_turn: bool,
        draft_checksum: str | None,
        prior_dependency_references: tuple[str, ...] = (),
        request_id: str,
    ) -> dict[str, object]:
        """Build one bounded Run delta without duplicating checkpoint history.

        The first durable turn imports the bounded legacy/session transcript.
        Every later turn sends only the new user input. Draft contents remain
        server-owned and are read through the Builder's scoped draft tools;
        only integrity metadata enters the prompt eagerly.
        """

        if (
            not isinstance(request, SkillDesignGenerationRequest)
            or not isinstance(turn_message, str)
            or not turn_message.strip()
            or len(turn_message) > 12_000
            or type(first_turn) is not bool
            or not isinstance(prior_dependency_references, tuple)
            or len(prior_dependency_references) > 64
            or any(not isinstance(item, str) or not item or len(item) > 512 for item in prior_dependency_references)
            or len(prior_dependency_references) != len(set(prior_dependency_references))
        ):
            raise AssetValidationFailed(request_id)
        conversation: dict[str, str] = {"mode": "initial", "brief": request.brief} if first_turn else {"mode": "continuation", "turn": turn_message.strip()}
        return {
            "skill_slug": request.skill_slug,
            "skill_name": request.skill_name,
            "conversation": conversation,
            "draft": {
                "checksum": draft_checksum,
                "files": [
                    {
                        "path": item.path,
                        "media_type": item.media_type,
                        "size_bytes": len(item.content.encode("utf-8")),
                        "sha256": hashlib.sha256(
                            item.content.encode("utf-8"),
                        ).hexdigest(),
                    }
                    for item in request.current_files
                ],
            },
            # These are persisted authoring requirements from the current
            # draft, not authority. The Builder must search/read/inspect them
            # again in this Run before it can finalize the next snapshot.
            "prior_dependency_references": sorted(
                prior_dependency_references,
            ),
            "attachments": [item.model_dump(mode="json") for item in request.attachments],
        }


__all__ = ["SkillBuilderRunAdmission", "SkillBuilderRunAdmissionService"]
