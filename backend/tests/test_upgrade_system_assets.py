from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType

import pytest
from sqlalchemy.exc import OperationalError

from app.shared_assets.bootstrap import BootstrapConflict, BootstrapResult
from deerflow.persistence.bootstrap import (
    SCHEMA_MUTATION_LOCK_KEY,
    M7RecreateRequired,
)

_DATABASE_URL = "postgresql://operator:do-not-print-this-password@db.example:5432/deerflow_test_operator"


@pytest.fixture
def upgrade_module() -> ModuleType:
    try:
        return importlib.import_module("scripts.upgrade_system_assets")
    except ModuleNotFoundError:
        pytest.fail(
            "the planned scripts/upgrade_system_assets.py operator entrypoint is missing",
            pytrace=False,
        )


class _Connection:
    def __init__(
        self,
        events: list[str],
        *,
        idle_session_timeout: str | None = None,
        unlock_result: bool = True,
        unlock_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.open = False
        self.locked = False
        self.idle_session_timeout = idle_session_timeout
        self.unlock_result = unlock_result
        self.unlock_error = unlock_error

    def _record(
        self,
        statement: object,
        parameters: dict[str, object] | None,
    ) -> str:
        sql = str(statement)
        values = dict(parameters or {})
        self.calls.append((sql, values))
        return sql

    def _advisory_result(self, sql: str) -> bool | None:
        if "pg_advisory_unlock" in sql:
            if self.unlock_error is not None:
                raise self.unlock_error
            if self.unlock_result:
                self.locked = False
                self.events.append("lock-released")
            else:
                self.events.append("lock-release-false")
            return self.unlock_result
        if ("pg_advisory_lock" in sql and "pg_advisory_xact_lock" not in sql) or "pg_try_advisory_lock" in sql:
            self.locked = True
            self.events.append("lock-acquired")
            return True
        return None

    async def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> object:
        sql = self._record(statement, parameters)
        return self._advisory_result(sql)

    async def scalar(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> object:
        sql = self._record(statement, parameters)
        if "current_setting" in sql:
            return self.idle_session_timeout
        return self._advisory_result(sql)


class _ConnectionContext:
    def __init__(
        self,
        connection: _Connection,
        *,
        enter_error: Exception | None = None,
        exit_error: Exception | None = None,
    ) -> None:
        self.connection = connection
        self.enter_error = enter_error
        self.exit_error = exit_error

    async def __aenter__(self) -> _Connection:
        self.connection.events.append("connection-enter")
        if self.enter_error is not None:
            raise self.enter_error
        self.connection.open = True
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        self.connection.open = False
        self.connection.locked = False
        self.connection.events.append("connection-exit")
        if self.exit_error is not None:
            raise self.exit_error


class _Engine:
    def __init__(
        self,
        name: str,
        *,
        idle_session_timeout: str | None = None,
        unlock_result: bool = True,
        unlock_error: Exception | None = None,
        enter_error: Exception | None = None,
        exit_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.events: list[str] = []
        self.connection = _Connection(
            self.events,
            idle_session_timeout=idle_session_timeout,
            unlock_result=unlock_result,
            unlock_error=unlock_error,
        )
        self.enter_error = enter_error
        self.exit_error = exit_error
        self.dispose_count = 0

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(
            self.connection,
            enter_error=self.enter_error,
            exit_error=self.exit_error,
        )

    async def dispose(self) -> None:
        self.dispose_count += 1
        self.events.append("dispose")


@dataclass(frozen=True)
class _Wiring:
    lock_engine: _Engine
    mutation_engine: _Engine
    engine_calls: list[tuple[tuple[object, ...], dict[str, object]]]
    bootstrap_calls: list[object]


def _wire_operator(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    database_state: str = "current",
    classify_error: Exception | None = None,
    bootstrap_error: Exception | None = None,
    idle_session_timeout: str | None = None,
    unlock_result: bool = True,
    unlock_error: Exception | None = None,
    lock_enter_error: Exception | None = None,
    lock_exit_error: Exception | None = None,
) -> _Wiring:
    lock_engine = _Engine(
        "lock",
        idle_session_timeout=idle_session_timeout,
        unlock_result=unlock_result,
        unlock_error=unlock_error,
        enter_error=lock_enter_error,
        exit_error=lock_exit_error,
    )
    mutation_engine = _Engine("mutation")
    engines = iter((lock_engine, mutation_engine))
    engine_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    bootstrap_calls: list[object] = []

    def create_engine(*args: object, **kwargs: object) -> _Engine:
        engine_calls.append((args, dict(kwargs)))
        try:
            return next(engines)
        except StopIteration:
            raise AssertionError("operator must create exactly two engines") from None

    async def classify(connection: _Connection) -> str:
        assert connection is lock_engine.connection
        assert connection.open is True
        assert connection.locked is True
        lock_engine.events.append("classify")
        if classify_error is not None:
            raise classify_error
        return database_state

    async def bootstrap(session_factory: object) -> BootstrapResult:
        assert lock_engine.connection.open is True
        assert lock_engine.connection.locked is True
        assert getattr(session_factory, "kw")["bind"] is mutation_engine
        assert getattr(session_factory, "kw")["expire_on_commit"] is False
        lock_engine.events.append("bootstrap")
        bootstrap_calls.append(session_factory)
        if bootstrap_error is not None:
            raise bootstrap_error
        return BootstrapResult(
            digest="a" * 64,
            counts={"agent": 1, "skill": 2, "mcp": 1},
            applied_changes=1,
        )

    monkeypatch.setattr(module, "create_async_engine", create_engine)
    monkeypatch.setattr(module, "classify_database", classify)
    monkeypatch.setattr(module, "bootstrap_system_assets", bootstrap)
    return _Wiring(
        lock_engine=lock_engine,
        mutation_engine=mutation_engine,
        engine_calls=engine_calls,
        bootstrap_calls=bootstrap_calls,
    )


def _session_lock_calls(
    connection: _Connection,
) -> tuple[tuple[str, dict[str, object]], ...]:
    return tuple((sql, parameters) for sql, parameters in connection.calls if "pg_advisory" in sql and "pg_advisory_xact_lock" not in sql)


@pytest.mark.asyncio
async def test_current_schema_holds_shared_session_lock_while_bootstrapping(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire_operator(monkeypatch, upgrade_module)

    result = await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    assert result.applied_changes == 1
    assert result.created == result.applied_changes
    assert len(wiring.bootstrap_calls) == 1
    significant_events = tuple(
        event
        for event in wiring.lock_engine.events
        if event
        in {
            "connection-enter",
            "lock-acquired",
            "classify",
            "bootstrap",
            "lock-released",
            "connection-exit",
            "dispose",
        }
    )
    assert significant_events == (
        "connection-enter",
        "lock-acquired",
        "classify",
        "bootstrap",
        "lock-released",
        "connection-exit",
        "dispose",
    )
    lock_calls = _session_lock_calls(wiring.lock_engine.connection)
    assert len(lock_calls) == 2
    acquire_sql, acquire_parameters = lock_calls[0]
    release_sql, release_parameters = lock_calls[1]
    assert "pg_advisory_lock" in acquire_sql
    assert "pg_advisory_xact_lock" not in acquire_sql
    assert "pg_advisory_unlock" in release_sql
    assert SCHEMA_MUTATION_LOCK_KEY in acquire_parameters.values()
    assert SCHEMA_MUTATION_LOCK_KEY in release_parameters.values()
    assert len(wiring.engine_calls) == 2
    lock_args, lock_kwargs = wiring.engine_calls[0]
    mutation_args, mutation_kwargs = wiring.engine_calls[1]
    assert lock_args == mutation_args
    assert lock_kwargs["isolation_level"] == "AUTOCOMMIT"
    assert "isolation_level" not in mutation_kwargs
    assert lock_kwargs["poolclass"] is mutation_kwargs["poolclass"]
    assert wiring.lock_engine.connection.locked is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
async def test_lock_session_disables_all_postgres_idle_timeouts(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        idle_session_timeout="10min",
    )

    await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    statements = tuple(sql for sql, _parameters in wiring.lock_engine.connection.calls)
    statement_timeout_index = statements.index("SET statement_timeout = 0")
    transaction_idle_index = statements.index("SET idle_in_transaction_session_timeout = 0")
    idle_probe_index = statements.index("SELECT current_setting('idle_session_timeout', true)")
    session_idle_index = statements.index("SET idle_session_timeout = 0")
    lock_index = next(index for index, sql in enumerate(statements) if "pg_advisory_lock" in sql and "pg_advisory_unlock" not in sql)
    assert statement_timeout_index < transaction_idle_index < idle_probe_index < session_idle_index < lock_index
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
async def test_partial_engine_creation_disposes_created_engine_without_leaking_credentials(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_engine = _Engine("lock")
    calls = 0

    def create_engine(*_args: object, **_kwargs: object) -> _Engine:
        nonlocal calls
        calls += 1
        if calls == 1:
            return lock_engine
        raise RuntimeError(f"leak-marker at {_DATABASE_URL}")

    monkeypatch.setattr(upgrade_module, "create_async_engine", create_engine)

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    public_error = str(exc_info.value)
    assert "leak-marker" not in public_error
    assert "do-not-print-this-password" not in public_error
    assert lock_engine.dispose_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["enter", "exit"])
async def test_lock_connection_context_errors_are_credential_safe(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    raw_error = RuntimeError(f"leak-marker {failure_phase} failure at {_DATABASE_URL}")
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        lock_enter_error=raw_error if failure_phase == "enter" else None,
        lock_exit_error=raw_error if failure_phase == "exit" else None,
    )

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    public_error = str(exc_info.value)
    assert "leak-marker" not in public_error
    assert "do-not-print-this-password" not in public_error
    assert _DATABASE_URL not in public_error
    assert len(wiring.bootstrap_calls) == (0 if failure_phase == "enter" else 1)
    if failure_phase == "exit":
        assert "最终状态无法确认" in public_error
        assert "幂等" in public_error
        assert "重跑" in public_error
        assert "无法连接" not in public_error
    assert wiring.lock_engine.connection.open is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unlock_result", "unlock_error"),
    [
        pytest.param(False, None, id="returned-false"),
        pytest.param(
            True,
            RuntimeError(f"leak-marker unlock failure at {_DATABASE_URL}"),
            id="raised-exception",
        ),
    ],
)
async def test_unlock_failure_reports_indeterminate_result_without_credentials(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    unlock_result: bool,
    unlock_error: Exception | None,
) -> None:
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        unlock_result=unlock_result,
        unlock_error=unlock_error,
    )

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    public_error = str(exc_info.value)
    assert "最终状态无法确认" in public_error
    assert "幂等" in public_error
    assert "重跑" in public_error
    assert "leak-marker" not in public_error
    assert "do-not-print-this-password" not in public_error
    assert _DATABASE_URL not in public_error
    assert len(wiring.bootstrap_calls) == 1
    assert wiring.lock_engine.connection.open is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_state", "bootstrap_error", "bootstrap_count"),
    [
        pytest.param("empty", None, 0, id="empty"),
        pytest.param("behind", None, 0, id="behind"),
        pytest.param(
            "current",
            BootstrapConflict(f"leak-marker conflict at {_DATABASE_URL}"),
            1,
            id="bootstrap-conflict",
        ),
    ],
)
@pytest.mark.parametrize(
    ("unlock_result", "unlock_error"),
    [
        pytest.param(False, None, id="unlock-returned-false"),
        pytest.param(
            True,
            RuntimeError(f"leak-marker unlock failure at {_DATABASE_URL}"),
            id="unlock-raised-exception",
        ),
    ],
)
async def test_primary_failure_plus_unlock_failure_never_claims_release_executed(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
    bootstrap_error: Exception | None,
    bootstrap_count: int,
    unlock_result: bool,
    unlock_error: Exception | None,
) -> None:
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        database_state=database_state,
        bootstrap_error=bootstrap_error,
        unlock_result=unlock_result,
        unlock_error=unlock_error,
    )

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    public_error = str(exc_info.value)
    assert "release 已执行" not in public_error
    assert "leak-marker" not in public_error
    assert "do-not-print-this-password" not in public_error
    assert _DATABASE_URL not in public_error
    assert len(wiring.bootstrap_calls) == bootstrap_count
    assert wiring.lock_engine.connection.open is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_state", "bootstrap_error", "primary_diagnostic", "bootstrap_count"),
    [
        pytest.param("empty", None, "setup-db", 0, id="empty"),
        pytest.param("behind", None, "upgrade-db", 0, id="behind"),
        pytest.param(
            "current",
            BootstrapConflict(f"leak-marker conflict at {_DATABASE_URL}"),
            "冲突",
            1,
            id="bootstrap-conflict",
        ),
    ],
)
async def test_primary_failure_plus_lock_exit_failure_preserves_primary_diagnostic(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
    bootstrap_error: Exception | None,
    primary_diagnostic: str,
    bootstrap_count: int,
) -> None:
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        database_state=database_state,
        bootstrap_error=bootstrap_error,
        lock_exit_error=RuntimeError(f"leak-marker lock exit failure at {_DATABASE_URL}"),
    )

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    public_error = str(exc_info.value)
    assert primary_diagnostic in public_error
    assert "release 已执行" not in public_error
    assert "leak-marker" not in public_error
    assert "do-not-print-this-password" not in public_error
    assert _DATABASE_URL not in public_error
    assert len(wiring.bootstrap_calls) == bootstrap_count
    assert wiring.lock_engine.connection.open is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_state", "guidance"),
    [
        pytest.param("empty", "setup-db", id="empty"),
        pytest.param("behind", "upgrade-db", id="behind"),
    ],
)
async def test_noncurrent_schema_fails_closed_without_bootstrap(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
    guidance: str,
) -> None:
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        database_state=database_state,
    )

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    assert guidance in str(exc_info.value)
    assert wiring.bootstrap_calls == []
    assert wiring.lock_engine.connection.locked is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
async def test_drifted_schema_fails_closed_without_bootstrap(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        classify_error=M7RecreateRequired(),
    )

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    assert "M7_RECREATE_REQUIRED" in str(exc_info.value)
    assert wiring.bootstrap_calls == []
    assert wiring.lock_engine.connection.locked is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bootstrap_error",
    [
        pytest.param(
            BootstrapConflict(
                f"leak-marker conflict at {_DATABASE_URL}",
            ),
            id="conflict",
        ),
        pytest.param(
            OperationalError(
                "INSERT INTO skills",
                {},
                RuntimeError(
                    f"leak-marker storage failure at {_DATABASE_URL}",
                ),
            ),
            id="storage",
        ),
    ],
)
async def test_bootstrap_failures_map_to_credential_safe_operator_errors(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap_error: Exception,
) -> None:
    wiring = _wire_operator(
        monkeypatch,
        upgrade_module,
        bootstrap_error=bootstrap_error,
    )

    with pytest.raises(upgrade_module.SystemAssetUpgradeError) as exc_info:
        await upgrade_module.upgrade_system_assets(_DATABASE_URL)

    public_error = str(exc_info.value)
    assert "leak-marker" not in public_error
    assert "do-not-print-this-password" not in public_error
    assert _DATABASE_URL not in public_error
    assert len(wiring.bootstrap_calls) == 1
    assert wiring.lock_engine.connection.locked is False
    assert wiring.lock_engine.dispose_count == 1
    assert wiring.mutation_engine.dispose_count == 1


def test_main_success_output_never_contains_dsn_or_credentials(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def upgrade(_database_url: str) -> BootstrapResult:
        return BootstrapResult(
            digest="b" * 64,
            counts={"agent": 1, "skill": 2, "mcp": 1},
            applied_changes=1,
        )

    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    monkeypatch.setattr(upgrade_module, "upgrade_system_assets", upgrade)

    assert upgrade_module.main([]) == 0

    output = capsys.readouterr()
    rendered = output.out + output.err
    assert rendered.strip()
    assert "do-not-print-this-password" not in rendered
    assert "operator" not in rendered
    assert _DATABASE_URL not in rendered


def test_main_without_database_url_returns_two_without_calling_upgrade(
    upgrade_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    async def upgrade(_database_url: str) -> BootstrapResult:
        nonlocal calls
        calls += 1
        raise AssertionError("upgrade must not run without DATABASE_URL")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(upgrade_module, "upgrade_system_assets", upgrade)

    assert upgrade_module.main([]) == 2

    output = capsys.readouterr()
    assert calls == 0
    assert "DATABASE_URL" in output.err
    assert output.out == ""
