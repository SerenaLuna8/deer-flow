"""Real-PostgreSQL checks for origin-specific Memory history authority."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from support.private_thread_seed import seed_private_thread_database

from deerflow.persistence.private_work.memory_document_model import (
    MemoryHistoryEntryRow,
)


def _tool_history(seed, *, prompt_version: str) -> MemoryHistoryEntryRow:
    tagged_text = "- [durable] remember contract fact"
    source_run_id = f"remember-run-{uuid.uuid4().hex}"
    return MemoryHistoryEntryRow(
        id=uuid.uuid4(),
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
        namespace="default",
        thread_id="remember-contract-thread",
        origin="tool",
        source_run_id=source_run_id,
        source_checkpoint_id=None,
        committed_checkpoint_id=None,
        source_digest=hashlib.sha256(source_run_id.encode()).hexdigest(),
        status="pending",
        tagged_text=tagged_text,
        content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
        preference_version=1,
        snip_prompt_version=prompt_version,
        summary_model_ref=None,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_tool_history_requires_the_exact_remember_prompt_version(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        async with seed.factory() as session, session.begin():
            session.add(
                _tool_history(
                    seed,
                    prompt_version="remember-tool-v1",
                )
            )

        with pytest.raises(IntegrityError):
            async with seed.factory() as session, session.begin():
                session.add(
                    _tool_history(
                        seed,
                        prompt_version="remember-tool-retired",
                    )
                )
    finally:
        await seed.engine.dispose()
