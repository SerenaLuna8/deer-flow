from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.authorization import PrivateRunAuthorizationService
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.run_repository import PrivateRunRepository
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from app.system_runtime_settings.models import RuntimePolicySection
from app.system_runtime_settings.repository import SystemRuntimePolicyRepository
from deerflow.agents.memory import format_memory_for_injection
from deerflow.config.memory_config import MemoryConfig
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryRecord,
    PrivateMemoryRepository,
)
from deerflow.persistence.private_work.memory_v2_recall import (
    MemoryV2RecallContract,
    MemoryV2RecallFact,
    MemoryV2RecallRepository,
    MemoryV2RecallSnapshot,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked

DEFAULT_PRIVATE_MEMORY_NAMESPACE = "default"


class PrivateRunMemoryAuthority:
    """Opaque, Worker-issued read authority for one Run's Memory snapshot."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        thread_id: str,
        namespace: str,
        memory_config: MemoryConfig | None = None,
        personalization_repository_builder=AccountPersonalizationRepository,
    ) -> None:
        context = require_issued_private_work_context(context)
        if type(claim) is not JobClaim or claim.run_id is None or claim.scope.project_id != context.project_id or claim.scope.owner_user_id != str(context.user_id):
            raise ValueError("Memory authority claim is invalid")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(namespace, str) or not namespace or namespace.strip() != namespace or len(namespace) > 255:
            raise ValueError("Memory authority coordinates are invalid")
        self._session_factory = session_factory
        self._context = context
        self._claim = claim
        self._thread_id = thread_id
        self._namespace = namespace
        if memory_config is not None and not isinstance(memory_config, MemoryConfig):
            raise ValueError("Memory authority configuration is invalid")
        self._memory_config = memory_config
        if not callable(personalization_repository_builder):
            raise ValueError("Memory authority configuration is invalid")
        self._personalization_repository_builder = personalization_repository_builder

    @property
    def pipeline_mode(self) -> str:
        """Expose only the frozen routing mode to trusted runtime middleware."""

        return self._memory_config.pipeline_mode if self._memory_config is not None else "consolidate"

    def _render_v2(self, facts: tuple[MemoryV2RecallFact, ...]) -> str:
        config = self._memory_config
        if config is None:
            raise RuntimeError("Memory v2 configuration is unavailable")
        rendered = format_memory_for_injection(
            {
                "facts": [
                    {
                        "id": str(fact.id),
                        "content": fact.content,
                        "category": fact.category,
                        "confidence": fact.confidence,
                        "createdAt": fact.created_at.isoformat(),
                    }
                    for fact in facts
                ]
            },
            max_tokens=config.max_injection_tokens,
            use_tiktoken=config.token_counting == "tiktoken",
            guaranteed_categories=config.guaranteed_categories,
            guaranteed_token_budget=config.guaranteed_token_budget,
        )
        if not rendered.strip():
            return ""
        return f"<memory>\n{rendered}\n</memory>"

    async def _load_v2(
        self,
        session,
    ) -> MemoryV2RecallSnapshot:
        config = self._memory_config
        if config is None or config.pipeline_mode != "v2":
            raise RuntimeError("Memory v2 configuration is unavailable")
        material = await SystemRuntimePolicyRepository(session).snapshot_material(
            project_id=self._context.project_id,
            owner_user_id=str(self._context.user_id),
            run_id=self._claim.run_id,
            section=RuntimePolicySection.AGENT_RUNTIME,
        )
        if material is None:
            raise AuthorizationRevoked
        _snapshot, version = material
        contract = MemoryV2RecallContract(
            policy_revision=int(version.version_number),
            max_facts=config.max_facts,
            token_budget=config.max_injection_tokens,
            guaranteed_categories=tuple(config.guaranteed_categories),
            guaranteed_token_budget=config.guaranteed_token_budget,
            use_tiktoken=config.token_counting == "tiktoken",
        )
        return await MemoryV2RecallRepository(session).load_or_create(
            self._context.resource_scope,
            namespace=self._namespace,
            thread_id=self._thread_id,
            run_id=self._claim.run_id,
            contract=contract,
            renderer=self._render_v2,
        )

    async def load_snapshot(
        self,
    ) -> PrivateMemoryRecord | MemoryV2RecallSnapshot | None:
        """Load without creating a row after one transactional authority check."""

        try:
            async with self._session_factory() as session, session.begin():
                current = await resolve_project_context_in_transaction(
                    session,
                    self._context.user_id,
                    self._context.project_id,
                    self._context.request_id,
                    lock=True,
                )
                if type(current) is not ProjectContext or current.membership_id != self._context.membership_id or current.membership_version != self._context.membership_version:
                    raise AuthorizationRevoked
                current.require(Capability.PRIVATE_WORK_READ_OWN)

                active = await PrivateRunAuthorizationService.is_active(
                    session,
                    project_id=self._context.project_id,
                    owner_user_id=str(self._context.user_id),
                    run_id=self._claim.run_id,
                    lock=False,
                )
                if not active:
                    raise AuthorizationRevoked

                runs = PrivateRunRepository(session)
                cancel_requested = await runs.assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id,
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                )
                if cancel_requested:
                    raise AuthorizationRevoked
                run = await runs.get(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id,
                    lock=False,
                )
                if run is None or run.thread_id != self._thread_id or run.job_id != self._claim.job_id:
                    raise AuthorizationRevoked

                if self._memory_config is not None and not self._memory_config.enabled:
                    return None
                preference = await self._personalization_repository_builder(session).read_memory(str(self._context.user_id))
                if not preference.memory_enabled:
                    return None
                if self.pipeline_mode == "v2":
                    return await self._load_v2(session)

                return await PrivateMemoryRepository(session).load(
                    scope=self._context.resource_scope,
                    namespace=self._namespace,
                    lock=True,
                )
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except Exception:
            raise AuthorizationRevoked from None


__all__ = [
    "DEFAULT_PRIVATE_MEMORY_NAMESPACE",
    "PrivateRunMemoryAuthority",
]
