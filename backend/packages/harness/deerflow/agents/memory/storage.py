"""Memory storage providers."""

import abc
import copy
import json
import logging
import math
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.config.agents_config import AGENT_NAME_PATTERN
from deerflow.config.memory_config import get_memory_config
from deerflow.config.paths import get_paths
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryFactRecord,
    PrivateMemoryFactWrite,
    PrivateMemoryRecord,
    PrivateMemoryRepository,
)
from deerflow.private_scope import PrivateResourceScope

logger = logging.getLogger(__name__)


def utc_now_iso_z() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix (matches prior naive-UTC output)."""
    return datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_empty_memory() -> dict[str, Any]:
    """Create an empty memory structure."""
    return {
        "version": "1.0",
        "lastUpdated": utc_now_iso_z(),
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


class MemoryStorage(abc.ABC):
    """Abstract base class for memory storage providers."""

    @abc.abstractmethod
    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Load memory data for the given agent."""
        pass

    @abc.abstractmethod
    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Force reload memory data for the given agent."""
        pass

    @abc.abstractmethod
    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> bool:
        """Save memory data for the given agent."""
        pass


class FileMemoryStorage(MemoryStorage):
    """File-based memory storage provider."""

    def __init__(self):
        """Initialize the file memory storage."""
        # Per-user/agent memory cache: keyed by (user_id, agent_name) tuple (None = global)
        # Value: (memory_data, file_mtime)
        self._memory_cache: dict[tuple[str | None, str | None], tuple[dict[str, Any], float | None]] = {}
        # Guards all reads and writes to _memory_cache across concurrent callers.
        self._cache_lock = threading.Lock()

    def _validate_agent_name(self, agent_name: str) -> None:
        """Validate that the agent name is safe to use in filesystem paths.

        Uses the repository's established AGENT_NAME_PATTERN to ensure consistency
        across the codebase and prevent path traversal or other problematic characters.
        """
        if not agent_name:
            raise ValueError("Agent name must be a non-empty string.")
        if not AGENT_NAME_PATTERN.match(agent_name):
            raise ValueError(f"Invalid agent name {agent_name!r}: names must match {AGENT_NAME_PATTERN.pattern}")

    def _get_memory_file_path(self, agent_name: str | None = None, *, user_id: str | None = None) -> Path:
        """Get the path to the memory file."""
        if user_id is not None:
            if agent_name is not None:
                self._validate_agent_name(agent_name)
                return get_paths().user_agent_memory_file(user_id, agent_name)
            config = get_memory_config()
            if config.storage_path and Path(config.storage_path).is_absolute():
                return Path(config.storage_path)
            return get_paths().user_memory_file(user_id)
        # Legacy: no user_id
        if agent_name is not None:
            self._validate_agent_name(agent_name)
            return get_paths().agent_memory_file(agent_name)
        config = get_memory_config()
        if config.storage_path:
            p = Path(config.storage_path)
            return p if p.is_absolute() else get_paths().base_dir / p
        return get_paths().memory_file

    def _load_memory_from_file(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Load memory data from file."""
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)

        if not file_path.exists():
            return create_empty_memory()

        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load memory file: %s", e)
            return create_empty_memory()

    @staticmethod
    def _cache_key(agent_name: str | None = None, *, user_id: str | None = None) -> tuple[str | None, str | None]:
        return (user_id, agent_name)

    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Load memory data (cached with file modification time check)."""
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            current_mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            current_mtime = None

        with self._cache_lock:
            cached = self._memory_cache.get(cache_key)
            if cached is not None and cached[1] == current_mtime:
                return cached[0]

        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)

        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, current_mtime)

        return memory_data

    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Reload memory data from file, forcing cache invalidation."""
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            mtime = None

        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, mtime)
        return memory_data

    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> bool:
        """Save memory data to file and update cache."""
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # Shallow-copy before adding lastUpdated so the caller's dict is not
            # mutated as a side-effect, and the cache reference is not silently
            # updated before the file write succeeds.
            memory_data = {**memory_data, "lastUpdated": utc_now_iso_z()}

            temp_path = file_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(memory_data, f, indent=2, ensure_ascii=False)

            temp_path.replace(file_path)

            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = None

            with self._cache_lock:
                self._memory_cache[cache_key] = (memory_data, mtime)
            logger.info("Memory saved to %s", file_path)
            return True
        except OSError as e:
            logger.error("Failed to save memory file: %s", e)
            return False


@dataclass(frozen=True, slots=True)
class ProjectMemorySnapshot:
    """Existing Memory JSON plus its PostgreSQL optimistic-lock version."""

    memory: dict[str, Any]
    version: int


class ProjectMemoryStorage:
    """Async project-owner scoped adapter over the PostgreSQL Memory repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _context_summary(memory_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(memory_data, dict):
            raise ValueError("memory data")
        empty = create_empty_memory()
        user = memory_data.get("user", empty["user"])
        history = memory_data.get("history", empty["history"])
        if not isinstance(user, dict) or not isinstance(history, dict):
            raise ValueError("memory data")
        version = memory_data.get("version", empty["version"])
        last_updated = memory_data.get("lastUpdated", empty["lastUpdated"])
        if not isinstance(version, str) or not isinstance(last_updated, str):
            raise ValueError("memory data")
        return {
            "version": version,
            "lastUpdated": last_updated,
            "user": copy.deepcopy(user),
            "history": copy.deepcopy(history),
        }

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @classmethod
    def _fact_write(cls, fact: object) -> PrivateMemoryFactWrite:
        if not isinstance(fact, dict):
            raise ValueError("memory fact")
        content = fact.get("content")
        category = fact.get("category", "context")
        confidence = fact.get("confidence", 0.5)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("memory fact content")
        if not isinstance(category, str) or not category.strip() or len(category.strip()) > 32:
            raise ValueError("memory fact category")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("memory fact confidence")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("memory fact confidence")
        try:
            fact_id = uuid.UUID(str(fact.get("id")))
        except (TypeError, ValueError):
            fact_id = uuid.uuid4()

        source_thread_id = fact.get("sourceThreadId")
        if source_thread_id is None:
            legacy_source = fact.get("source")
            if isinstance(legacy_source, str) and legacy_source not in {"", "manual", "unknown"}:
                source_thread_id = legacy_source
        source_run_id = fact.get("sourceRunId")
        if source_thread_id is not None and (not isinstance(source_thread_id, str) or not source_thread_id):
            raise ValueError("memory fact source thread")
        if source_run_id is not None and (not isinstance(source_run_id, str) or not source_run_id or source_thread_id is None):
            raise ValueError("memory fact source run")
        return PrivateMemoryFactWrite(
            id=fact_id,
            content=content.strip(),
            category=category.strip(),
            confidence=confidence,
            source_thread_id=source_thread_id,
            source_run_id=source_run_id,
            created_at=cls._parse_datetime(fact.get("createdAt")),
        )

    @staticmethod
    def _iso_z(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"

    @classmethod
    def _fact_json(cls, fact: PrivateMemoryFactRecord) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": str(fact.id),
            "content": fact.content,
            "category": fact.category,
            "confidence": fact.confidence,
            "createdAt": cls._iso_z(fact.created_at),
            "source": fact.source_thread_id or "manual",
        }
        if fact.source_thread_id is not None:
            result["sourceThreadId"] = fact.source_thread_id
        if fact.source_run_id is not None:
            result["sourceRunId"] = fact.source_run_id
        return result

    @classmethod
    def _snapshot(cls, record: PrivateMemoryRecord) -> ProjectMemorySnapshot:
        empty = create_empty_memory()
        context = record.context_summary
        memory = {
            "version": copy.deepcopy(context.get("version", empty["version"])),
            "lastUpdated": copy.deepcopy(context.get("lastUpdated", empty["lastUpdated"])),
            "user": copy.deepcopy(context.get("user", empty["user"])),
            "history": copy.deepcopy(context.get("history", empty["history"])),
            "facts": [cls._fact_json(fact) for fact in record.facts],
        }
        return ProjectMemorySnapshot(memory=memory, version=record.version)

    async def create_if_needed(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        empty = create_empty_memory()
        async with self._session_factory() as session, session.begin():
            record = await PrivateMemoryRepository(session).create_if_needed(
                scope=scope,
                namespace=namespace,
                context_summary=self._context_summary(empty),
            )
        return self._snapshot(record)

    async def load(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        return await self.create_if_needed(scope=scope, namespace=namespace)

    async def reload(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        return await self.load(scope=scope, namespace=namespace)

    async def save(
        self,
        memory_data: dict[str, Any],
        *,
        scope: PrivateResourceScope,
        namespace: str,
        expected_version: int,
    ) -> ProjectMemorySnapshot:
        facts = memory_data.get("facts", []) if isinstance(memory_data, dict) else None
        if not isinstance(facts, list):
            raise ValueError("memory facts")
        fact_writes = tuple(self._fact_write(fact) for fact in facts)
        async with self._session_factory() as session, session.begin():
            record = await PrivateMemoryRepository(session).save(
                scope=scope,
                namespace=namespace,
                context_summary=self._context_summary(memory_data),
                facts=fact_writes,
                expected_version=expected_version,
            )
        return self._snapshot(record)

    async def clear(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
        expected_version: int,
    ) -> ProjectMemorySnapshot:
        empty = create_empty_memory()
        async with self._session_factory() as session, session.begin():
            record = await PrivateMemoryRepository(session).clear(
                scope=scope,
                namespace=namespace,
                context_summary=self._context_summary(empty),
                expected_version=expected_version,
            )
        return self._snapshot(record)


_storage_instance: MemoryStorage | None = None
_storage_lock = threading.Lock()


def get_memory_storage() -> MemoryStorage:
    """Get the configured memory storage instance."""
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    with _storage_lock:
        if _storage_instance is not None:
            return _storage_instance

        config = get_memory_config()
        storage_class_path = config.storage_class

        try:
            module_path, class_name = storage_class_path.rsplit(".", 1)
            import importlib

            module = importlib.import_module(module_path)
            storage_class = getattr(module, class_name)

            # Validate that the configured storage is a MemoryStorage implementation
            if not isinstance(storage_class, type):
                raise TypeError(f"Configured memory storage '{storage_class_path}' is not a class: {storage_class!r}")
            if not issubclass(storage_class, MemoryStorage):
                raise TypeError(f"Configured memory storage '{storage_class_path}' is not a subclass of MemoryStorage")

            _storage_instance = storage_class()
        except Exception as e:
            logger.error(
                "Failed to load memory storage %s, falling back to FileMemoryStorage: %s",
                storage_class_path,
                e,
            )
            _storage_instance = FileMemoryStorage()

    return _storage_instance
