#!/usr/bin/env python3
"""Build deterministic, metadata-only inventories of SQLite source files."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TableInventory:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    row_count: int
    digest: str


@dataclass(frozen=True)
class SQLiteInventory:
    path: str
    sha256: str
    size_bytes: int
    integrity: str
    tables: tuple[TableInventory, ...]


class InventoryError(RuntimeError):
    """Raised when a SQLite source cannot be safely inventoried."""


def open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _hash_part(digest: Any, tag: bytes, payload: bytes) -> None:
    digest.update(struct.pack(">I", len(tag)))
    digest.update(tag)
    digest.update(struct.pack(">Q", len(payload)))
    digest.update(payload)


def _hash_text(digest: Any, tag: bytes, value: str) -> None:
    _hash_part(digest, tag, value.encode("utf-8"))


def _hash_value(digest: Any, sqlite_type: str, value: object) -> None:
    _hash_text(digest, b"sqlite-runtime-type", sqlite_type)
    if value is None:
        _hash_part(digest, b"python-none", b"")
    elif isinstance(value, bool):
        _hash_part(digest, b"python-bool", b"1" if value else b"0")
    elif isinstance(value, int):
        _hash_part(digest, b"python-int", str(value).encode("ascii"))
    elif isinstance(value, float):
        _hash_part(digest, b"python-float", struct.pack(">d", value))
    elif isinstance(value, str):
        _hash_text(digest, b"python-str", value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        _hash_part(digest, b"python-bytes", bytes(value))
    else:
        raise InventoryError(f"Unsupported SQLite value type: {type(value).__name__}")


def _table_info(connection: sqlite3.Connection, table_name: str) -> list[tuple[Any, ...]]:
    quoted_table = _quote_identifier(table_name)
    return list(connection.execute(f"PRAGMA table_info({quoted_table})"))


def table_digest(connection: sqlite3.Connection, table_name: str) -> str:
    """Return a deterministic digest of a table's schema and rows."""
    table_info = _table_info(connection, table_name)
    if not table_info:
        raise InventoryError(f"Table does not exist: {table_name}")

    digest = hashlib.sha256()
    _hash_text(digest, b"table", table_name)
    for _cid, name, declared_type, _not_null, _default, primary_key_position in table_info:
        _hash_text(digest, b"column-name", name)
        _hash_text(digest, b"declared-type", declared_type)
        _hash_part(digest, b"primary-key-position", struct.pack(">I", primary_key_position))

    columns = [row[1] for row in table_info]
    primary_key = [row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5] > 0]
    primary_key_set = set(primary_key)
    order_columns = primary_key + [column for column in columns if column not in primary_key_set] if primary_key else columns
    quoted_columns = [_quote_identifier(column) for column in columns]
    value_expressions = ", ".join(quoted_columns)
    type_expressions = ", ".join(f"typeof({column})" for column in quoted_columns)
    order_expressions = []
    for column in order_columns:
        quoted_column = _quote_identifier(column)
        order_expressions.extend((f"{quoted_column} COLLATE BINARY", f"typeof({quoted_column})"))
    query = f"SELECT {value_expressions}, {type_expressions} FROM {_quote_identifier(table_name)} ORDER BY {', '.join(order_expressions)}"
    for row in connection.execute(query):
        _hash_part(digest, b"row-start", b"")
        values = row[: len(columns)]
        sqlite_types = row[len(columns) :]
        for sqlite_type, value in zip(sqlite_types, values, strict=True):
            _hash_value(digest, sqlite_type, value)
        _hash_part(digest, b"row-end", b"")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_sqlite(path: Path) -> SQLiteInventory:
    """Inspect one SQLite file without modifying it."""
    resolved_path = path.resolve()
    if not resolved_path.is_file():
        raise InventoryError(f"SQLite source does not exist: {resolved_path}")

    try:
        size_bytes = resolved_path.stat().st_size
        sha256 = _file_sha256(resolved_path)
        with open_read_only(resolved_path) as connection:
            connection.execute("PRAGMA query_only = ON")
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity = "\n".join(str(row[0]) for row in integrity_rows)
            if integrity != "ok":
                raise InventoryError(f"SQLite integrity check failed for {resolved_path}: {integrity}")

            table_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name COLLATE BINARY")]
            tables = []
            for table_name in table_names:
                table_info = _table_info(connection, table_name)
                columns = tuple(row[1] for row in table_info)
                primary_key = tuple(row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5] > 0)
                row_count = connection.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}").fetchone()[0]
                tables.append(
                    TableInventory(
                        name=table_name,
                        columns=columns,
                        primary_key=primary_key,
                        row_count=row_count,
                        digest=table_digest(connection, table_name),
                    )
                )
    except InventoryError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise InventoryError(f"Unable to inspect SQLite source {resolved_path}: {error}") from error

    return SQLiteInventory(
        path=str(resolved_path),
        sha256=sha256,
        size_bytes=size_bytes,
        integrity=integrity,
        tables=tuple(tables),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="SQLite source file paths")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        inventories = [asdict(inspect_sqlite(path)) for path in args.paths]
    except InventoryError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(inventories, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
