from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol, TypeVar, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
    strip_private_client_fields,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from deerflow.persistence.private_work.model import PrivateArtifactRow, PrivateFileRow
from deerflow.runtime.private_scope import PrivateResourceScope

PRIVATE_SCOPE_MARKER = "deerflow_private_scope"
_T = TypeVar("_T")


class PrivateCheckpointQuotaPort(Protocol):
    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None: ...


class _NoopPrivateCheckpointQuota:
    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None:
        del session, scope, file_id, size, request_id


def _drop_marker(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _drop_marker(item) for key, item in value.items() if isinstance(key, str) and key != PRIVATE_SCOPE_MARKER}
    if isinstance(value, list):
        return [_drop_marker(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_drop_marker(item) for item in value)
    return value


class ProjectScopedCheckpointer:
    """Factory that binds raw checkpoint persistence to trusted project context."""

    def __init__(
        self,
        raw_saver: BaseCheckpointSaver,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        quota: PrivateCheckpointQuotaPort | None = None,
    ) -> None:
        self._raw = raw_saver
        self._session_factory = session_factory
        self._quota = quota or _NoopPrivateCheckpointQuota()
        try:
            self._owner_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("ProjectScopedCheckpointer must be created on its owning event loop") from exc

    def for_context(self, context: PrivateWorkContext) -> _ScopedCheckpointSaver:
        return _ScopedCheckpointSaver(
            self._raw,
            self._session_factory,
            require_issued_private_work_context(context),
            self._owner_loop,
            self._quota,
        )


class _ScopedCheckpointSaver(BaseCheckpointSaver):
    def __init__(
        self,
        raw_saver: BaseCheckpointSaver,
        session_factory: async_sessionmaker[AsyncSession],
        context: PrivateWorkContext,
        owner_loop: asyncio.AbstractEventLoop,
        quota: PrivateCheckpointQuotaPort,
    ) -> None:
        super().__init__(serde=raw_saver.serde)
        self._raw = raw_saver
        self._session_factory = session_factory
        self._context = context
        self._owner_loop = owner_loop
        self._quota = quota
        self._revalidator = PrivateWorkRevalidator()
        self._authorization_boundary: object | None = None

    def set_authorization_boundary(self, boundary: object) -> None:
        self._authorization_boundary = boundary

    def already_authorized(
        self,
        session: AsyncSession,
    ) -> _AlreadyAuthorizedCheckpointSaver:
        """Bind graph checkpoint operations to one caller-owned transaction."""
        return _AlreadyAuthorizedCheckpointSaver(self, session)

    @property
    def config_specs(self):
        return self._raw.config_specs

    def get_next_version(self, current, channel):
        return self._raw.get_next_version(current, channel)

    @property
    def _scope_marker(self) -> dict[str, str]:
        context = require_issued_private_work_context(self._context)
        return {
            "project_id": str(context.project_id),
            "owner_user_id": str(context.user_id),
        }

    @staticmethod
    def _thread_id(config: RunnableConfig | None) -> str:
        if not isinstance(config, Mapping):
            raise PrivateWorkNotFound("unknown")
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise PrivateWorkNotFound("unknown")
        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise PrivateWorkNotFound("unknown")
        return thread_id

    def _sanitize_config(
        self,
        config: RunnableConfig,
        *,
        thread_id: str | None = None,
    ) -> RunnableConfig:
        clean = cast(
            dict[str, object],
            _drop_marker(strip_private_client_fields(cast(Mapping[str, object], config))),
        )
        configurable = clean.get("configurable")
        clean_configurable = dict(configurable) if isinstance(configurable, Mapping) else {}
        clean_configurable["thread_id"] = thread_id or self._thread_id(config)
        clean["configurable"] = clean_configurable
        return cast(RunnableConfig, clean)

    def _sanitize_metadata(
        self,
        metadata: CheckpointMetadata,
    ) -> CheckpointMetadata:
        clean = cast(
            dict[str, Any],
            _drop_marker(strip_private_client_fields(cast(Mapping[str, object], metadata))),
        )
        clean[PRIVATE_SCOPE_MARKER] = self._scope_marker
        return cast(CheckpointMetadata, clean)

    def _validate_marker(
        self,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> None:
        if self._thread_id(item.config) != thread_id:
            raise PrivateWorkNotFound(self._context.request_id)
        marker = item.metadata.get(PRIVATE_SCOPE_MARKER)
        if marker != self._scope_marker:
            raise PrivateWorkNotFound(self._context.request_id)

    @asynccontextmanager
    async def _locked_active(
        self,
        thread_id: str,
        capability: Capability,
        authorization_operation: str,
    ) -> AsyncIterator[None]:
        try:
            if self._authorization_boundary is not None:
                await getattr(self._authorization_boundary, authorization_operation)()
            async with self._session_factory() as session:
                async with session.begin():
                    if self._authorization_boundary is None:
                        await self._revalidator.require(
                            session,
                            self._context,
                            capability,
                            lock=True,
                        )
                    record = await PrivateThreadRepository(session).get(
                        scope=self._context.resource_scope,
                        thread_id=thread_id,
                        lock=True,
                    )
                    if record is None:
                        raise PrivateWorkNotFound(self._context.request_id)
                    yield
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_READ_OWN,
            "before_checkpoint_read",
        ):
            try:
                item = await self._raw.aget_tuple(clean_config)
            except PrivateWorkError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None
            if item is not None:
                self._validate_marker(item, thread_id=thread_id)
            return item

    async def aget_tuple_already_authorized(
        self,
        config: RunnableConfig,
        *,
        session: AsyncSession,
    ) -> CheckpointTuple | None:
        """Read through the raw saver while the caller holds the scoped DB locks."""

        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        try:
            item = await self._raw.aget_tuple(clean_config)
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        if item is not None:
            self._validate_marker(item, thread_id=thread_id)
        return item

    async def aget(self, config: RunnableConfig) -> Checkpoint | None:
        item = await self.aget_tuple(config)
        return None if item is None else item.checkpoint

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        clean_before = None if before is None else self._sanitize_config(before, thread_id=thread_id)
        clean_filter = (
            None
            if filter is None
            else cast(
                dict[str, Any],
                _drop_marker(strip_private_client_fields(filter)),
            )
        )
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_READ_OWN,
            "before_checkpoint_read",
        ):
            try:
                async for item in self._raw.alist(
                    clean_config,
                    filter=clean_filter,
                    before=clean_before,
                    limit=limit,
                ):
                    self._validate_marker(item, thread_id=thread_id)
                    yield item
            except PrivateWorkError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None

    async def alist_already_authorized(
        self,
        config: RunnableConfig | None,
        *,
        session: AsyncSession,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List raw tuples while the caller holds the scoped Thread lock."""
        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        clean_before = None if before is None else self._sanitize_config(before, thread_id=thread_id)
        clean_filter = (
            None
            if filter is None
            else cast(
                dict[str, Any],
                _drop_marker(strip_private_client_fields(filter)),
            )
        )
        try:
            async for item in self._raw.alist(
                clean_config,
                filter=clean_filter,
                before=clean_before,
                limit=limit,
            ):
                self._validate_marker(item, thread_id=thread_id)
                yield item
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = self._thread_id(config)
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_CREATE,
            "before_checkpoint_write",
        ):
            try:
                written_config = await self._raw.aput(
                    self._sanitize_config(config, thread_id=thread_id),
                    checkpoint,
                    self._sanitize_metadata(metadata),
                    new_versions,
                )
                item = await self._raw.aget_tuple(written_config)
                if item is None:
                    raise PrivateWorkNotFound(self._context.request_id)
                self._validate_marker(item, thread_id=thread_id)
                return written_config
            except PrivateWorkError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aput_already_authorized(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
        *,
        session: AsyncSession,
    ) -> RunnableConfig:
        """Write while the caller holds the exact scoped Thread authority lock.

        This narrow adapter exists for compare-and-swap flows that must keep the
        final head check and checkpoint write inside one database lock cycle.
        The caller must already hold the scoped Thread lock; this method repeats
        the exact authority/row check in the same transaction as defense in
        depth before touching the raw saver.
        """

        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        try:
            await self._revalidator.require(
                session,
                self._context,
                Capability.PRIVATE_WORK_CREATE,
                lock=True,
            )
            record = await PrivateThreadRepository(session).get(
                scope=self._context.resource_scope,
                thread_id=thread_id,
                lock=True,
            )
            if record is None:
                raise PrivateWorkNotFound(self._context.request_id)
            written_config = await self._raw.aput(
                self._sanitize_config(config, thread_id=thread_id),
                checkpoint,
                self._sanitize_metadata(metadata),
                new_versions,
            )
            item = await self._raw.aget_tuple(written_config)
            if item is None:
                raise PrivateWorkNotFound(self._context.request_id)
            self._validate_marker(item, thread_id=thread_id)
            return written_config
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_CREATE,
            "before_checkpoint_write",
        ):
            try:
                item = await self._raw.aget_tuple(clean_config)
                if item is not None:
                    self._validate_marker(item, thread_id=thread_id)
                # Pending writes are a first-class saver operation: LangGraph
                # may emit them before the matching checkpoint row exists.
                await self._raw.aput_writes(
                    clean_config,
                    writes,
                    task_id,
                    task_path,
                )
            except PrivateWorkError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aput_writes_already_authorized(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
        *,
        session: AsyncSession,
    ) -> None:
        """Write pending channel values inside a caller-owned transaction."""
        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        try:
            await self._revalidator.require(
                session,
                self._context,
                Capability.PRIVATE_WORK_CREATE,
                lock=True,
            )
            record = await PrivateThreadRepository(session).get(
                scope=self._context.resource_scope,
                thread_id=thread_id,
                lock=True,
            )
            if record is None:
                raise PrivateWorkNotFound(self._context.request_id)
            item = await self._raw.aget_tuple(clean_config)
            if item is not None:
                self._validate_marker(item, thread_id=thread_id)
            await self._raw.aput_writes(
                clean_config,
                writes,
                task_id,
                task_path,
            )
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def adelete_thread(
        self,
        thread_id: str,
        *,
        expected_version: int | None = None,
    ) -> None:
        context = require_issued_private_work_context(self._context)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_READ_OWN,
                        lock=True,
                    )
                    repository = PrivateThreadRepository(session)
                    record = await repository.get(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        lock=True,
                    )
                    if record is None:
                        raise PrivateWorkNotFound(context.request_id)
                    if expected_version is not None and record.version != expected_version:
                        raise PrivateWorkConflict(context.request_id)
                    deleted = await repository.mark_deleted(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        expected_version=record.version,
                    )
                    ready_files = (
                        (
                            await session.execute(
                                select(PrivateFileRow)
                                .where(
                                    PrivateFileRow.project_id == context.project_id,
                                    PrivateFileRow.owner_user_id == str(context.user_id),
                                    PrivateFileRow.thread_id == thread_id,
                                    PrivateFileRow.status == "ready",
                                )
                                .with_for_update(of=PrivateFileRow)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for file_row in ready_files:
                        await self._quota.release_file(
                            session,
                            context.resource_scope,
                            file_id=file_row.id,
                            size=file_row.size,
                            request_id=context.request_id,
                        )
                    await session.execute(
                        update(PrivateFileRow)
                        .where(
                            PrivateFileRow.project_id == context.project_id,
                            PrivateFileRow.owner_user_id == str(context.user_id),
                            PrivateFileRow.thread_id == thread_id,
                            PrivateFileRow.status != "deleted",
                        )
                        .values(
                            status="deleted",
                            deleted_at=deleted.deleted_at,
                            updated_at=deleted.deleted_at,
                        )
                    )
                    await session.execute(
                        update(PrivateArtifactRow)
                        .where(
                            PrivateArtifactRow.project_id == context.project_id,
                            PrivateArtifactRow.owner_user_id == str(context.user_id),
                            PrivateArtifactRow.thread_id == thread_id,
                            PrivateArtifactRow.deleted_at.is_(None),
                        )
                        .values(deleted_at=deleted.deleted_at)
                    )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        try:
            await self._raw.adelete_thread(thread_id)
        except Exception:
            await self._set_delete_status(thread_id, "retry_required")
            raise PrivateWorkUnavailable(context.request_id) from None
        await self._set_delete_status(thread_id, "complete")

    async def _set_delete_status(self, thread_id: str, status: str) -> None:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await PrivateThreadRepository(session).set_checkpoint_delete_status(
                        scope=self._context.resource_scope,
                        thread_id=thread_id,
                        status=status,
                    )
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    def _run_sync(self, coroutine_factory: Callable[[], Awaitable[_T]]) -> _T:
        if not self._owner_loop.is_running():
            raise PrivateWorkUnavailable(self._context.request_id)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._owner_loop:
            raise PrivateWorkUnavailable(self._context.request_id)
        return cast(
            _T,
            asyncio.run_coroutine_threadsafe(
                coroutine_factory(),
                self._owner_loop,
            ).result(),
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._run_sync(lambda: self.aget_tuple(config))

    def get(self, config: RunnableConfig) -> Checkpoint | None:
        return self._run_sync(lambda: self.aget(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        async def collect() -> list[CheckpointTuple]:
            return [
                item
                async for item in self.alist(
                    config,
                    filter=filter,
                    before=before,
                    limit=limit,
                )
            ]

        return iter(self._run_sync(collect))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self._run_sync(lambda: self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._run_sync(lambda: self.aput_writes(config, writes, task_id, task_path))

    def delete_thread(
        self,
        thread_id: str,
        *,
        expected_version: int | None = None,
    ) -> None:
        self._run_sync(
            lambda: self.adelete_thread(
                thread_id,
                expected_version=expected_version,
            )
        )


class _AlreadyAuthorizedCheckpointSaver(BaseCheckpointSaver):
    """Async saver facade pinned to one existing SQL transaction."""

    def __init__(
        self,
        saver: _ScopedCheckpointSaver,
        session: AsyncSession,
    ) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver
        self._session = session

    @property
    def config_specs(self):
        return self._saver.config_specs

    def get_next_version(self, current, channel):
        return self._saver.get_next_version(current, channel)

    async def aget_tuple(
        self,
        config: RunnableConfig,
    ) -> CheckpointTuple | None:
        return await self._saver.aget_tuple_already_authorized(
            config,
            session=self._session,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for item in self._saver.alist_already_authorized(
            config,
            session=self._session,
            filter=filter,
            before=before,
            limit=limit,
        ):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await self._saver.aput_already_authorized(
            config,
            checkpoint,
            metadata,
            new_versions,
            session=self._session,
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._saver.aput_writes_already_authorized(
            config,
            writes,
            task_id,
            task_path,
            session=self._session,
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise PrivateWorkUnavailable(self._saver._context.request_id)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        del config, filter, before, limit
        raise PrivateWorkUnavailable(self._saver._context.request_id)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del config, checkpoint, metadata, new_versions
        raise PrivateWorkUnavailable(self._saver._context.request_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, writes, task_id, task_path
        raise PrivateWorkUnavailable(self._saver._context.request_id)
