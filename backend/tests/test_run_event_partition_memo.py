from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from deerflow.persistence.models.run_event import (
    RunEventRow,
    _ensure_run_event_month_partition,
)


class _Dialect:
    name = "postgresql"


class _Transaction:
    pass


class _Connection:
    def __init__(self) -> None:
        self.dialect = _Dialect()
        self.info: dict[str, object] = {}
        self.root_transaction: _Transaction | None = _Transaction()
        self.nested_transaction: _Transaction | None = None
        self.calls: list[dict[str, datetime]] = []
        self.fail_once = False

    def get_transaction(self) -> _Transaction | None:
        return self.root_transaction

    def get_nested_transaction(self) -> _Transaction | None:
        return self.nested_transaction

    def execute(self, _statement, parameters: dict[str, datetime]) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("partition ensure failed")
        self.calls.append(parameters)


def _insert_listener(connection: _Connection, created_at: datetime) -> None:
    _ensure_run_event_month_partition(
        None,
        connection,
        RunEventRow(created_at=created_at),
    )


def test_partition_ensure_is_memoized_by_transaction_and_utc_month() -> None:
    connection = _Connection()
    china_standard_time = timezone(timedelta(hours=8))

    _insert_listener(connection, datetime(2026, 8, 1, tzinfo=UTC))
    _insert_listener(
        connection,
        datetime(2026, 9, 1, 1, tzinfo=china_standard_time),
    )
    _insert_listener(connection, datetime(2026, 9, 1, tzinfo=UTC))

    assert [call["created_at"] for call in connection.calls] == [
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    ]


def test_partition_ensure_is_repeated_for_a_new_transaction() -> None:
    connection = _Connection()
    created_at = datetime(2026, 8, 1, tzinfo=UTC)

    _insert_listener(connection, created_at)
    connection.root_transaction = _Transaction()
    _insert_listener(connection, created_at)

    assert len(connection.calls) == 2


def test_partition_ensure_does_not_survive_nested_transaction_boundary() -> None:
    connection = _Connection()
    created_at = datetime(2026, 8, 1, tzinfo=UTC)

    _insert_listener(connection, created_at)
    connection.nested_transaction = _Transaction()
    _insert_listener(connection, created_at)
    _insert_listener(connection, created_at)
    connection.nested_transaction = None
    _insert_listener(connection, created_at)

    assert len(connection.calls) == 3


def test_failed_partition_ensure_is_not_memoized() -> None:
    connection = _Connection()
    connection.fail_once = True
    created_at = datetime(2026, 8, 1, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="partition ensure failed"):
        _insert_listener(connection, created_at)
    _insert_listener(connection, created_at)

    assert len(connection.calls) == 1
