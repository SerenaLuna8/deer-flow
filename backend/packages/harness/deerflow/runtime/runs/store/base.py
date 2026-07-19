"""Abstract interface for run metadata storage.

RunManager depends on this interface. Implementations:
- RunRepository: PostgreSQL implementation
- Future: RunRepository backed by SQLAlchemy ORM

All methods accept an optional user_id for user isolation.
When user_id is None, no user filtering is applied (single-user mode).
"""

from __future__ import annotations

import abc
from typing import Any

from deerflow.runtime.private_scope import PrivateResourceScope


class RunStore(abc.ABC):
    @abc.abstractmethod
    async def put(
        self,
        run_id: str,
        *,
        thread_id: str,
        assistant_id: str | None = None,
        user_id: str | None = None,
        scope: PrivateResourceScope | None = None,
        model_name: str | None = None,
        status: str = "pending",
        multitask_strategy: str = "reject",
        metadata: dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        error: str | None = None,
        created_at: str | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def get(
        self,
        run_id: str,
        *,
        user_id: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> dict[str, Any] | None:
        pass

    @abc.abstractmethod
    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        scope: PrivateResourceScope | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> bool | None:
        """Update a run status.

        Returns ``False`` when the store can prove no row was updated. Older or
        lightweight stores may return ``None`` when they cannot report rowcount.
        """
        pass

    async def update_status_authoritative(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> dict[str, Any] | bool | None:
        """Write status and optionally return the store's authoritative outcome.

        Legacy stores inherit the boolean/``None`` result from ``update_status``.
        Stores that transform writes atomically may return ``status`` and ``error``.
        """

        if scope is None:
            return await self.update_status(
                run_id,
                status,
                error=error,
            )
        return await self.update_status(run_id, status, error=error, scope=scope)

    @abc.abstractmethod
    async def delete(
        self,
        run_id: str,
        *,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def update_model_name(
        self,
        run_id: str,
        model_name: str | None,
        *,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Update the model_name field for an existing run."""
        pass

    @abc.abstractmethod
    async def update_run_completion(
        self,
        run_id: str,
        *,
        status: str,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_tokens: int = 0,
        llm_call_count: int = 0,
        lead_agent_tokens: int = 0,
        subagent_tokens: int = 0,
        middleware_tokens: int = 0,
        token_usage_by_model: dict[str, dict[str, int]] | None = None,
        message_count: int = 0,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
        error: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> bool | None:
        """Persist final completion fields.

        Returns ``False`` when the store can prove no row was updated.
        """
        pass

    async def update_run_progress(
        self,
        run_id: str,
        *,
        total_input_tokens: int | None = None,
        total_output_tokens: int | None = None,
        total_tokens: int | None = None,
        llm_call_count: int | None = None,
        lead_agent_tokens: int | None = None,
        subagent_tokens: int | None = None,
        middleware_tokens: int | None = None,
        token_usage_by_model: dict[str, dict[str, int]] | None = None,
        message_count: int | None = None,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Persist a best-effort running snapshot without changing run status."""
        return None

    @abc.abstractmethod
    async def list_pending(
        self,
        *,
        before: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def list_inflight(
        self,
        *,
        before: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict[str, Any]]:
        """Return persisted runs that are still ``pending`` or ``running``."""
        pass

    async def list_inflight_trusted_unscoped(
        self,
        *,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Trusted startup/cutover scan; never expose through product entrypoints."""
        raise NotImplementedError

    @abc.abstractmethod
    async def aggregate_tokens_by_thread(
        self,
        thread_id: str,
        *,
        include_active: bool = False,
        scope: PrivateResourceScope | None = None,
    ) -> dict[str, Any]:
        """Aggregate token usage for completed runs in a thread.

        Returns a dict with keys: total_tokens, total_input_tokens,
        total_output_tokens, total_runs, by_model (model_name → {tokens, runs}),
        by_caller ({lead_agent, subagent, middleware}).
        """
        pass
