"""The ``relex_document`` handler: rebuild lexical derivations in place.

``lexical_tsv`` and ``lexical_version`` are pure derivations of the persisted
model text, so a lexical algorithm change never needs the original file, the
parser, or the Embedding Provider. One claim locks the exact Task and Document,
re-derives every current-generation Segment and Child row from
``stored_model_text`` in a Worker thread, and writes the new columns under the
same lock — content edits serialize behind it, and a Document that moved to a
newer generation after admission makes the claim a no-op. The Document stays
``ready`` throughout; this handler never publishes, never changes vectors, and
never marks a Document failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..asyncio_utils import run_sync_to_completion
from ..contracts import KNOWLEDGE_LEXICAL_VERSION, KNOWLEDGE_STORAGE_UNAVAILABLE, KNOWLEDGE_TASK_FAILED, KnowledgeError
from ..persistence.derivations import stored_model_text
from ..persistence.models import KnowledgeDocumentRow, KnowledgeSegmentChildRow, KnowledgeSegmentRow
from ..persistence.tasks import settle_task_row_success
from ..retrieval.lexical import lexical_index_input
from ..tasks.worker import KnowledgeTaskClaim, ProjectActiveCheck
from .progress import KnowledgeTaskProgressReporter, ensure_locked_task_lease, lock_indexing_claim

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Derivation:
    row_id: UUID
    lexical_input: str


def _derive(rows: list[tuple[UUID, str, str]], parsing_profile: dict | None) -> list[_Derivation]:
    """CPU-bound tokenization for one document; runs off the event loop."""

    return [
        _Derivation(
            row_id,
            lexical_index_input(stored_model_text(content=content, index_text=index_text, parsing_profile=parsing_profile)),
        )
        for row_id, content, index_text in rows
    ]


class KnowledgeRelexHandler:
    """Process one ``relex_document`` claim end to end."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        project_active_check: ProjectActiveCheck | None,
    ) -> None:
        self._session_factory = session_factory
        self._project_active_check = project_active_check

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        progress = KnowledgeTaskProgressReporter(self._session_factory, claim, project_active_check=self._project_active_check)
        await progress.advance_stage("loading_segments")
        try:
            async with self._session_factory() as session, session.begin():
                task = await lock_indexing_claim(session, claim, project_active_check=self._project_active_check)
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id, KnowledgeDocumentRow.project_id == claim.project_id).with_for_update())
                await ensure_locked_task_lease(session, task)
                claim_deadline = task.lease_until
                if claim_deadline is None:
                    raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
                moment = datetime.now(UTC)
                if document is None or document.status != "ready" or document.version != claim.target_version or document.published_version != document.version:
                    # A retry, reparse, or deletion admitted after this task
                    # owns the rows now; its publish derives them itself.
                    settle_task_row_success(task, now=moment)
                    return
                segment_rows = (
                    await session.execute(
                        select(KnowledgeSegmentRow.id, KnowledgeSegmentRow.content, KnowledgeSegmentRow.index_text).where(
                            KnowledgeSegmentRow.knowledge_document_id == document.id,
                            KnowledgeSegmentRow.document_version == document.version,
                        )
                    )
                ).all()
                child_rows = (
                    await session.execute(
                        select(KnowledgeSegmentChildRow.id, KnowledgeSegmentChildRow.content, KnowledgeSegmentChildRow.index_text).where(
                            KnowledgeSegmentChildRow.knowledge_document_id == document.id,
                            KnowledgeSegmentChildRow.document_version == document.version,
                        )
                    )
                ).all()
                # The Document lock is held while a thread tokenizes, so a
                # concurrent Segment edit (which derives its own lexical
                # fields for the new content) cannot interleave with these
                # writes and leave a row carrying tokens of older text.
                segments = await run_sync_to_completion(_derive, [tuple(row) for row in segment_rows], document.parsing_profile)
                children = await run_sync_to_completion(_derive, [tuple(row) for row in child_rows], document.parsing_profile)
                await ensure_locked_task_lease(session, task)
                for model, derivations in ((KnowledgeSegmentRow, segments), (KnowledgeSegmentChildRow, children)):
                    for derivation in derivations:
                        await session.execute(
                            update(model)
                            .where(model.id == derivation.row_id)
                            .values(
                                lexical_tsv=func.to_tsvector("simple", derivation.lexical_input),
                                lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                            )
                        )
                total = len(segments) + len(children)
                task.completed_units = total
                task.total_units = total
                settle_task_row_success(task, now=moment)
                await session.flush()
                # The transaction owns the Task row, so comparing the original
                # deadline to PostgreSQL time is the final authority boundary.
                now = await session.scalar(select(func.clock_timestamp()))
                if not isinstance(now, datetime) or now.tzinfo is None or claim_deadline <= now:
                    raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            # SQL exceptions can carry segment text through query parameters.
            logger.warning("knowledge relex database operation failed")
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用") from None
