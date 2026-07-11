from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.sqlite_inventory import InventoryError, inspect_sqlite, open_read_only, table_digest

SCRIPT = Path(__file__).parents[1] / "scripts" / "sqlite_inventory.py"


def _database(path: Path, statements: tuple[str, ...]) -> Path:
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
    return path


def test_inspect_sqlite_returns_stably_sorted_user_table_metadata(tmp_path: Path):
    path = _database(
        tmp_path / "source.sqlite",
        (
            'CREATE TABLE "zeta" (payload BLOB)',
            'CREATE TABLE "alpha" (note TEXT, id INTEGER PRIMARY KEY AUTOINCREMENT)',
            "INSERT INTO \"alpha\" VALUES ('secret-row-value', 2)",
            "INSERT INTO \"alpha\" VALUES ('another-value', 1)",
        ),
    )

    inventory = inspect_sqlite(path)

    assert inventory.path == str(path.resolve())
    assert len(inventory.sha256) == 64
    assert inventory.size_bytes == path.stat().st_size
    assert inventory.integrity == "ok"
    assert tuple(table.name for table in inventory.tables) == ("alpha", "zeta")
    assert inventory.tables[0].columns == ("note", "id")
    assert inventory.tables[0].primary_key == ("id",)
    assert inventory.tables[0].row_count == 2
    assert len(inventory.tables[0].digest) == 64


def test_inspection_and_read_only_connection_do_not_modify_source(tmp_path: Path):
    path = _database(tmp_path / "source.sqlite", ("CREATE TABLE items (id INTEGER)",))
    before = (path.stat().st_mtime_ns, path.read_bytes())

    inspect_sqlite(path)
    with open_read_only(path) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO items VALUES (1)")

    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


def test_missing_source_raises_inventory_error(tmp_path: Path):
    with pytest.raises(InventoryError, match="does not exist"):
        inspect_sqlite(tmp_path / "missing.sqlite")


def test_corrupt_source_raises_inventory_error(tmp_path: Path):
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"this is not a sqlite database")

    with pytest.raises(InventoryError, match="SQLite"):
        inspect_sqlite(path)


def test_composite_primary_key_uses_declared_key_order_for_stable_digest(tmp_path: Path):
    first = _database(
        tmp_path / "first.sqlite",
        (
            "CREATE TABLE entries (b INTEGER, a TEXT, value TEXT, PRIMARY KEY (a, b))",
            "INSERT INTO entries VALUES (2, 'x', 'two')",
            "INSERT INTO entries VALUES (1, 'x', 'one')",
        ),
    )
    second = _database(
        tmp_path / "second.sqlite",
        (
            "CREATE TABLE entries (b INTEGER, a TEXT, value TEXT, PRIMARY KEY (a, b))",
            "INSERT INTO entries VALUES (1, 'x', 'one')",
            "INSERT INTO entries VALUES (2, 'x', 'two')",
        ),
    )

    first_table = inspect_sqlite(first).tables[0]
    second_table = inspect_sqlite(second).tables[0]

    assert first_table.primary_key == ("a", "b")
    assert first_table.digest == second_table.digest


def test_table_without_primary_key_sorts_by_all_columns_for_stable_digest(tmp_path: Path):
    first = _database(
        tmp_path / "first.sqlite",
        ("CREATE TABLE entries (a TEXT, b INTEGER)", "INSERT INTO entries VALUES ('z', 2)", "INSERT INTO entries VALUES ('a', 1)"),
    )
    second = _database(
        tmp_path / "second.sqlite",
        ("CREATE TABLE entries (a TEXT, b INTEGER)", "INSERT INTO entries VALUES ('a', 1)", "INSERT INTO entries VALUES ('z', 2)"),
    )

    assert inspect_sqlite(first).tables[0].digest == inspect_sqlite(second).tables[0].digest


def test_digest_distinguishes_null_empty_string_bytes_and_text(tmp_path: Path):
    digests = []
    for index, value in enumerate((None, "", b"", "same", b"same")):
        path = _database(tmp_path / f"source-{index}.sqlite", ("CREATE TABLE values_table (value)",))
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO values_table VALUES (?)", (value,))
        with open_read_only(path) as connection:
            digests.append(table_digest(connection, "values_table"))

    assert len(set(digests)) == 5


def test_digest_distinguishes_column_names_and_declared_types(tmp_path: Path):
    digests = set()
    for index, definition in enumerate(("value TEXT", "value BLOB", "other TEXT")):
        path = _database(
            tmp_path / f"schema-{index}.sqlite",
            (f"CREATE TABLE values_table ({definition})", "INSERT INTO values_table VALUES ('x')"),
        )
        with open_read_only(path) as connection:
            digests.add(table_digest(connection, "values_table"))

    assert len(digests) == 3


def test_table_name_containing_double_quote_is_safely_inspected(tmp_path: Path):
    path = _database(
        tmp_path / "source.sqlite",
        ('CREATE TABLE "odd""name" ("key""part" TEXT PRIMARY KEY)', 'INSERT INTO "odd""name" VALUES (\'value\')'),
    )

    table = inspect_sqlite(path).tables[0]

    assert table.name == 'odd"name'
    assert table.columns == ('key"part',)
    assert table.row_count == 1


def test_cli_emits_json_metadata_for_one_or_more_sources_without_row_contents(tmp_path: Path):
    first = _database(tmp_path / "one.sqlite", ("CREATE TABLE items (secret TEXT)", "INSERT INTO items VALUES ('do-not-leak')"))
    second = _database(tmp_path / "two.sqlite", ("CREATE TABLE other (id INTEGER PRIMARY KEY)",))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(first), str(second)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert [item["path"] for item in payload] == [str(first.resolve()), str(second.resolve())]
    assert payload[0]["tables"][0]["columns"] == ["secret"]
    assert "do-not-leak" not in result.stdout


def test_cli_returns_one_and_json_error_for_failure(tmp_path: Path):
    missing = tmp_path / "missing.sqlite"

    result = subprocess.run([sys.executable, str(SCRIPT), str(missing)], check=False, capture_output=True, text=True)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "does not exist" in json.loads(result.stderr)["error"]
