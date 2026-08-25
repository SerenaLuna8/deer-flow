"""Architecture contract for the incremental Memory repository split."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock

import pytest

from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
)
from deerflow.persistence.private_work.memory_dream_store import MemoryDreamStore
from deerflow.persistence.private_work.memory_repository_parts import (
    MemoryDocumentReader,
    MemoryDocumentStore,
    MemoryEpisodeReader,
    MemoryHistoryRepository,
)


class _CallerOwnedSession:
    """Opaque session sentinel; components must borrow rather than replace it."""


def test_memory_repository_components_share_the_exact_caller_session() -> None:
    session = _CallerOwnedSession()

    repository = MemoryDocumentRepository(session)  # type: ignore[arg-type]

    assert isinstance(repository.documents, MemoryDocumentReader)
    assert isinstance(repository.documents, MemoryDocumentStore)
    assert isinstance(repository.episodes, MemoryEpisodeReader)
    assert isinstance(repository.history, MemoryHistoryRepository)
    assert isinstance(repository.dreams, MemoryDreamStore)
    assert repository.session is session
    assert repository.documents.session is session
    assert repository.episodes.session is session
    assert repository.history.session is session
    assert repository.dreams.session is session
    assert repository.dreams.jobs is repository.jobs
    assert repository.dreams.documents is repository.documents


@pytest.mark.asyncio
async def test_memory_facade_delegates_reads_without_opening_a_transaction() -> None:
    session = _CallerOwnedSession()
    repository = MemoryDocumentRepository(session)  # type: ignore[arg-type]
    expected = object()
    repository.documents.read_state = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await repository.read_state(object(), for_update=True)  # type: ignore[arg-type]

    assert result is expected
    repository.documents.read_state.assert_awaited_once_with(  # type: ignore[attr-defined]
        ANY,
        for_update=True,
    )


@pytest.mark.asyncio
async def test_memory_facade_delegates_history_writes_to_shared_session_component() -> None:
    session = _CallerOwnedSession()
    repository = MemoryDocumentRepository(session)  # type: ignore[arg-type]
    activation_result = object()
    proposal_result = object()
    repository.history.activate = AsyncMock(return_value=activation_result)  # type: ignore[method-assign]
    repository.history.propose = AsyncMock(return_value=proposal_result)  # type: ignore[method-assign]
    activation = object()
    proposal = object()

    assert await repository.activate_history(activation) is activation_result  # type: ignore[arg-type]
    assert await repository.propose_entry(proposal) is proposal_result  # type: ignore[arg-type]
    repository.history.activate.assert_awaited_once_with(activation)  # type: ignore[attr-defined]
    repository.history.propose.assert_awaited_once_with(proposal)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_memory_facade_delegates_episode_search_with_all_boundaries() -> None:
    session = _CallerOwnedSession()
    repository = MemoryDocumentRepository(session)  # type: ignore[arg-type]
    expected = (object(),)
    repository.episodes.search = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    now = object()

    result = await repository.search_episodes(  # type: ignore[arg-type]
        object(),
        query="needle",
        tags=("durable",),
        limit=3,
        retention_days=365,
        now=now,  # type: ignore[arg-type]
    )

    assert result is expected
    repository.episodes.search.assert_awaited_once_with(  # type: ignore[attr-defined]
        ANY,
        query="needle",
        tags=("durable",),
        limit=3,
        retention_days=365,
        now=now,
    )


@pytest.mark.asyncio
async def test_memory_facade_delegates_restore_to_document_store() -> None:
    repository = MemoryDocumentRepository(_CallerOwnedSession())  # type: ignore[arg-type]
    expected = object()
    repository.documents.restore_version = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    now = object()

    result = await repository.restore_version(  # type: ignore[arg-type]
        object(),
        target_version=2,
        expected_current_version=4,
        expected_sections=("one", "two"),
        max_tokens=1_000,
        now=now,  # type: ignore[arg-type]
    )

    assert result is expected
    repository.documents.restore_version.assert_awaited_once_with(  # type: ignore[attr-defined]
        ANY,
        target_version=2,
        expected_current_version=4,
        expected_sections=("one", "two"),
        max_tokens=1_000,
        now=now,
    )


@pytest.mark.asyncio
async def test_memory_facade_delegates_dream_lifecycle_to_one_store() -> None:
    repository = MemoryDocumentRepository(_CallerOwnedSession())  # type: ignore[arg-type]
    expected = object()
    now = object()
    repository.dreams.list_due_scopes = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    repository.dreams.list_budget_rewrite_scope_page = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    repository.dreams.is_scope_due = AsyncMock(return_value=True)  # type: ignore[method-assign]
    repository.dreams.admit_dream = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    repository.dreams.load_dream_work = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    repository.dreams.finalize_dream = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    repository.dreams.release_dream = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    scope = object()
    job_id = object()

    assert (
        await repository.list_due_scopes(  # type: ignore[arg-type]
            now=now, interval_minutes=120, limit=7
        )
        is expected
    )
    assert (
        await repository.list_budget_rewrite_scope_page(
            budget_tokens=1_000,
            admissible_roles=("admin",),
            cursor=None,
            limit=8,
        )
        is expected
    )
    assert await repository.is_scope_due(  # type: ignore[arg-type]
        scope, now=now, interval_minutes=120
    )
    assert (
        await repository.admit_dream(  # type: ignore[arg-type]
            scope,
            account_private_generation=object(),
            trigger="manual_dream",
            frozen=object(),
            initial_content=None,
            initial_sections=None,
            sections_policy_version_id=None,
            now=now,
            max_attempts=4,
        )
        is expected
    )
    assert await repository.load_dream_work(scope, job_id) is expected  # type: ignore[arg-type]
    assert (
        await repository.finalize_dream(  # type: ignore[arg-type]
            scope,
            job_id=job_id,
            lease_token="lease",
            expected_history_digest="digest",
            expected_base_version=2,
            expected_base_digest="base",
            expected_sections=("one", "two"),
            content="content",
            now=now,
            episode_retention_days=365,
        )
        is expected
    )
    assert (
        await repository.release_dream(  # type: ignore[arg-type]
            scope,
            job_id=job_id,
            lease_token="lease",
            now=now,
            cancelled=False,
            public_error_code="MEMORY_DREAM_FAILED",
            retryable=True,
            retry_initial_seconds=5,
            retry_max_seconds=300,
        )
        is expected
    )

    repository.dreams.list_due_scopes.assert_awaited_once_with(  # type: ignore[attr-defined]
        now=now, interval_minutes=120, limit=7
    )
    repository.dreams.list_budget_rewrite_scope_page.assert_awaited_once_with(  # type: ignore[attr-defined]
        budget_tokens=1_000,
        admissible_roles=("admin",),
        cursor=None,
        limit=8,
    )
    repository.dreams.is_scope_due.assert_awaited_once_with(  # type: ignore[attr-defined]
        scope, now=now, interval_minutes=120
    )
    repository.dreams.admit_dream.assert_awaited_once()
    repository.dreams.load_dream_work.assert_awaited_once_with(scope, job_id)  # type: ignore[attr-defined]
    repository.dreams.finalize_dream.assert_awaited_once()
    repository.dreams.release_dream.assert_awaited_once()
