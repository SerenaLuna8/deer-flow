"""Stable per-Run recall snapshots for the project-private Memory v2 path."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.private_work.memory_v2_model import (
    MemoryFactRevisionRow,
    MemoryFactRow,
    RunMemoryContextItemRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.private_scope import PrivateResourceScope


class MemoryV2RecallInvalid(RuntimeError):
    """The trusted recall contract or persisted snapshot is inconsistent."""


@dataclass(frozen=True, slots=True)
class MemoryV2RecallContract:
    """Frozen selection and rendering inputs for one admitted v2 Run."""

    policy_revision: int
    max_facts: int
    token_budget: int
    guaranteed_categories: tuple[str, ...]
    guaranteed_token_budget: int
    use_tiktoken: bool
    pipeline_mode: Literal["v2"] = "v2"
    selection_version: str = "v2-active-confidence-v1"
    renderer_version: str = "v2-facts-v1"
    prompt_version: str = "hidden-human-memory-v1"


@dataclass(frozen=True, slots=True)
class MemoryV2RecallFact:
    """One exact Fact Revision pinned by a Run snapshot item."""

    id: uuid.UUID
    revision_id: uuid.UUID
    revision_sequence: int
    content: str
    content_digest: str
    category: str
    confidence: float
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryV2RecallSnapshot:
    """Model-visible overlay of one immutable ordered Run snapshot."""

    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    thread_id: str
    run_id: str
    version: int
    pipeline_mode: Literal["v2"]
    facts: tuple[MemoryV2RecallFact, ...]
    rendered_content: str
    rendered_content_digest: str
    content_erased: bool
    created_at: datetime

    @property
    def retrieval_at(self) -> datetime:
        """Freeze time-decay ranking for retry/resume of this Run."""

        return self.created_at


MemoryV2RecallRenderer = Callable[[tuple[MemoryV2RecallFact, ...]], str]


def _scope(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
    if type(scope) is not PrivateResourceScope:
        raise MemoryV2RecallInvalid("Memory recall scope is invalid")
    try:
        return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
    except (TypeError, ValueError):
        raise MemoryV2RecallInvalid("Memory recall scope is invalid") from None


def _namespace(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 255:
        raise MemoryV2RecallInvalid("Memory recall namespace is invalid")
    return value


def _coordinate(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 64:
        raise MemoryV2RecallInvalid(f"Memory recall {name} is invalid")
    return value


def _contract(value: MemoryV2RecallContract) -> MemoryV2RecallContract:
    if type(value) is not MemoryV2RecallContract:
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if value.pipeline_mode != "v2" or type(value.policy_revision) is not int or value.policy_revision < 1:
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if type(value.max_facts) is not int or not 1 <= value.max_facts <= 500:
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if type(value.token_budget) is not int or not 0 <= value.token_budget <= 8_000:
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if type(value.guaranteed_token_budget) is not int or not 0 <= value.guaranteed_token_budget <= 2_000:
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if type(value.use_tiktoken) is not bool:
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if not isinstance(value.guaranteed_categories, tuple) or any(not isinstance(category, str) or not category or category != category.strip() or len(category) > 32 for category in value.guaranteed_categories):
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if len(value.guaranteed_categories) != len(set(value.guaranteed_categories)):
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    if any(
        not isinstance(version, str) or not version or version != version.strip() or len(version) > 64
        for version in (
            value.selection_version,
            value.renderer_version,
            value.prompt_version,
        )
    ):
        raise MemoryV2RecallInvalid("Memory recall contract is invalid")
    return value


class MemoryV2RecallRepository:
    """Session-bound authority for one Run's ordered v2 recall snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _predicates(
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        row_type,
    ) -> tuple:
        return (
            row_type.project_id == project_id,
            row_type.owner_user_id == owner_user_id,
            row_type.namespace == namespace,
        )

    async def _lock_run(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
    ) -> None:
        locked = await self.session.scalar(
            select(RunRow.run_id)
            .where(
                RunRow.project_id == project_id,
                RunRow.owner_user_id == owner_user_id,
                RunRow.thread_id == thread_id,
                RunRow.run_id == run_id,
            )
            .with_for_update(of=RunRow)
        )
        if locked != run_id:
            raise MemoryV2RecallInvalid("Memory recall Run is unavailable")

    async def _existing(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        run_id: str,
    ) -> RunMemoryContextSnapshotRow | None:
        return (
            await self.session.execute(
                select(RunMemoryContextSnapshotRow)
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        RunMemoryContextSnapshotRow,
                    ),
                    RunMemoryContextSnapshotRow.run_id == run_id,
                )
                .with_for_update(of=RunMemoryContextSnapshotRow)
            )
        ).scalar_one_or_none()

    async def _revision_ceiling(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
    ) -> int:
        value = await self.session.scalar(
            select(func.coalesce(func.max(MemoryFactRevisionRow.revision_sequence), 0)).where(
                *self._predicates(
                    project_id,
                    owner_user_id,
                    namespace,
                    MemoryFactRevisionRow,
                )
            )
        )
        return int(value or 0)

    async def _initial_facts(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        ceiling: int,
        contract: MemoryV2RecallContract,
    ) -> tuple[MemoryV2RecallFact, ...]:
        guaranteed_rank = case(
            (
                MemoryFactRevisionRow.category.in_(contract.guaranteed_categories),
                0,
            ),
            else_=1,
        )
        rows = tuple(
            (
                await self.session.execute(
                    select(MemoryFactRow, MemoryFactRevisionRow)
                    .join(
                        MemoryFactRevisionRow,
                        and_(
                            MemoryFactRevisionRow.project_id == MemoryFactRow.project_id,
                            MemoryFactRevisionRow.owner_user_id == MemoryFactRow.owner_user_id,
                            MemoryFactRevisionRow.namespace == MemoryFactRow.namespace,
                            MemoryFactRevisionRow.fact_id == MemoryFactRow.id,
                            MemoryFactRevisionRow.id == MemoryFactRow.current_revision_id,
                        ),
                    )
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryFactRow,
                        ),
                        MemoryFactRow.status == "active",
                        MemoryFactRevisionRow.revision_sequence <= ceiling,
                        MemoryFactRevisionRow.content.is_not(None),
                        MemoryFactRevisionRow.content_erased_at.is_(None),
                    )
                    .order_by(
                        guaranteed_rank,
                        MemoryFactRevisionRow.confidence.desc(),
                        MemoryFactRevisionRow.revision_sequence.desc(),
                        MemoryFactRow.id,
                    )
                    .limit(contract.max_facts)
                )
            ).all()
        )
        return tuple(self._fact(fact, revision) for fact, revision in rows)

    async def _visible_items(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        snapshot_id: uuid.UUID,
    ) -> tuple[MemoryV2RecallFact, ...]:
        rows = tuple(
            (
                await self.session.execute(
                    select(
                        RunMemoryContextItemRow,
                        MemoryFactRow,
                        MemoryFactRevisionRow,
                    )
                    .join(
                        MemoryFactRow,
                        and_(
                            MemoryFactRow.project_id == RunMemoryContextItemRow.project_id,
                            MemoryFactRow.owner_user_id == RunMemoryContextItemRow.owner_user_id,
                            MemoryFactRow.namespace == RunMemoryContextItemRow.namespace,
                            MemoryFactRow.id == RunMemoryContextItemRow.fact_id,
                        ),
                    )
                    .join(
                        MemoryFactRevisionRow,
                        and_(
                            MemoryFactRevisionRow.project_id == RunMemoryContextItemRow.project_id,
                            MemoryFactRevisionRow.owner_user_id == RunMemoryContextItemRow.owner_user_id,
                            MemoryFactRevisionRow.namespace == RunMemoryContextItemRow.namespace,
                            MemoryFactRevisionRow.fact_id == RunMemoryContextItemRow.fact_id,
                            MemoryFactRevisionRow.id == RunMemoryContextItemRow.revision_id,
                            MemoryFactRevisionRow.content_digest == RunMemoryContextItemRow.content_digest,
                        ),
                    )
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            RunMemoryContextItemRow,
                        ),
                        RunMemoryContextItemRow.snapshot_id == snapshot_id,
                    )
                    .order_by(RunMemoryContextItemRow.ordinal)
                )
            ).all()
        )
        return tuple(self._fact(fact, revision) for _item, fact, revision in rows if fact.status == "active" and revision.content is not None and revision.content_erased_at is None)

    @staticmethod
    def _fact(
        fact: MemoryFactRow,
        revision: MemoryFactRevisionRow,
    ) -> MemoryV2RecallFact:
        if revision.content is None or revision.content_erased_at is not None:
            raise MemoryV2RecallInvalid("Memory recall Fact content is unavailable")
        return MemoryV2RecallFact(
            id=fact.id,
            revision_id=revision.id,
            revision_sequence=int(revision.revision_sequence),
            content=revision.content,
            content_digest=revision.content_digest,
            category=revision.category,
            confidence=float(revision.confidence),
            created_at=revision.created_at,
        )

    async def _create(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        thread_id: str,
        run_id: str,
        ceiling: int,
        facts: tuple[MemoryV2RecallFact, ...],
        rendered_content: str,
        contract: MemoryV2RecallContract,
    ) -> RunMemoryContextSnapshotRow:
        snapshot = RunMemoryContextSnapshotRow(
            id=uuid.uuid4(),
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            thread_id=thread_id,
            run_id=run_id,
            pipeline_mode="v2",
            fact_revision_ceiling=ceiling,
            summary_id=None,
            summary_revision=None,
            selection_version=contract.selection_version,
            renderer_version=contract.renderer_version,
            prompt_version=contract.prompt_version,
            policy_revision=contract.policy_revision,
            token_budget=contract.token_budget,
            rendered_content=rendered_content,
            rendered_content_digest=hashlib.sha256(rendered_content.encode("utf-8")).hexdigest(),
            content_erased_at=None,
        )
        self.session.add(snapshot)
        await self.session.flush()
        self.session.add_all(
            RunMemoryContextItemRow(
                id=uuid.uuid4(),
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
                snapshot_id=snapshot.id,
                ordinal=ordinal,
                fact_id=fact.id,
                revision_id=fact.revision_id,
                rank_score=fact.confidence,
                selection_reason=("guaranteed_category" if fact.category in contract.guaranteed_categories else "active_fact"),
                content_digest=fact.content_digest,
            )
            for ordinal, fact in enumerate(facts)
        )
        await self.session.flush()
        return snapshot

    async def load_or_create(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        thread_id: str,
        run_id: str,
        contract: MemoryV2RecallContract,
        renderer: MemoryV2RecallRenderer,
    ) -> MemoryV2RecallSnapshot:
        """Load the pinned ordered items or create them exactly once per Run."""

        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        thread_id = _coordinate(thread_id, name="Thread")
        run_id = _coordinate(run_id, name="Run")
        contract = _contract(contract)
        if not callable(renderer):
            raise MemoryV2RecallInvalid("Memory recall renderer is invalid")

        await self._lock_run(
            project_id=project_id,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            run_id=run_id,
        )
        snapshot = await self._existing(
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            run_id=run_id,
        )
        if snapshot is None:
            ceiling = await self._revision_ceiling(
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
            )
            facts = await self._initial_facts(
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
                ceiling=ceiling,
                contract=contract,
            )
            rendered = await asyncio.to_thread(renderer, facts)
            if not isinstance(rendered, str) or len(rendered) > 128_000:
                raise MemoryV2RecallInvalid("Memory recall renderer output is invalid")
            snapshot = await self._create(
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
                thread_id=thread_id,
                run_id=run_id,
                ceiling=ceiling,
                facts=facts,
                rendered_content=rendered,
                contract=contract,
            )
        elif (
            snapshot.thread_id != thread_id
            or snapshot.pipeline_mode != "v2"
            or int(snapshot.policy_revision) != contract.policy_revision
            or snapshot.selection_version != contract.selection_version
            or snapshot.renderer_version != contract.renderer_version
            or snapshot.prompt_version != contract.prompt_version
        ):
            raise MemoryV2RecallInvalid("Memory recall snapshot contract is inconsistent")

        visible_facts = await self._visible_items(
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            snapshot_id=snapshot.id,
        )
        rendered = await asyncio.to_thread(renderer, visible_facts)
        if not isinstance(rendered, str) or len(rendered) > 128_000:
            raise MemoryV2RecallInvalid("Memory recall renderer output is invalid")
        return MemoryV2RecallSnapshot(
            id=snapshot.id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            thread_id=thread_id,
            run_id=run_id,
            version=int(snapshot.fact_revision_ceiling),
            pipeline_mode="v2",
            facts=visible_facts,
            rendered_content=rendered,
            rendered_content_digest=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            content_erased=snapshot.content_erased_at is not None,
            created_at=snapshot.created_at,
        )


__all__ = [
    "MemoryV2RecallContract",
    "MemoryV2RecallFact",
    "MemoryV2RecallInvalid",
    "MemoryV2RecallRepository",
    "MemoryV2RecallSnapshot",
]
