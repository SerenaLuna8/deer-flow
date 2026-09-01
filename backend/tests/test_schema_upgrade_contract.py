from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.persistence.final_schema_contract import (
    FINAL_SCHEMA_V1_CATALOG_SIGNATURE,
    LANGGRAPH_ROOT_OBJECTS,
)

SCHEMA_COMMENTS_PLACEHOLDER = "-- INCLUDE GENERATED SCHEMA COMMENTS FROM schema_comments.sql"


@pytest.fixture
def schema_upgrade_module():
    return importlib.import_module("deerflow.persistence.schema_upgrade")


def _migration(
    tmp_path: Path,
    *,
    source_revision: str,
    target_revision: str,
    filename: str,
    payload: bytes | None = None,
) -> SimpleNamespace:
    sql_path = tmp_path / filename
    sql_path.write_bytes(
        payload if payload is not None else (b"ALTER TABLE users ADD COLUMN future_example INTEGER;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n"),
    )
    return SimpleNamespace(
        source_revision=source_revision,
        target_revision=target_revision,
        sql_path=sql_path,
        source_catalog_signature=FINAL_SCHEMA_V1_CATALOG_SIGNATURE,
        source_inventory_digest="0" * 64,
    )


def test_schema_upgrade_registry_is_empty_while_schema_v1_is_current(
    schema_upgrade_module,
) -> None:
    assert schema_upgrade_module.CURRENT_SCHEMA_REVISION == "schema_v1"
    assert tuple(schema_upgrade_module.MIGRATIONS) == ()
    schema_upgrade_module.validate_schema_upgrade_artifacts()


def test_source_inventory_digest_treats_optional_langgraph_as_one_schema_revision(
    schema_upgrade_module,
) -> None:
    app_inventory = frozenset(
        {
            "relation:r:users",
            "index:users_pkey:users",
        },
    )

    assert schema_upgrade_module.schema_inventory_digest(
        app_inventory,
    ) == schema_upgrade_module.schema_inventory_digest(
        app_inventory | LANGGRAPH_ROOT_OBJECTS,
    )


def test_schema_upgrade_registry_rejects_a_head_bump_without_a_migration(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schema_upgrade_module,
        "CURRENT_SCHEMA_REVISION",
        "schema_v2",
    )

    with pytest.raises(RuntimeError, match="baseline|migration|upgrade|schema"):
        schema_upgrade_module.validate_schema_upgrade_artifacts()


def test_schema_upgrade_registry_accepts_one_future_linear_migration(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration = _migration(
        tmp_path,
        source_revision="schema_v1",
        target_revision="schema_v2",
        filename="schema_v1_to_schema_v2.sql",
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", (migration,))
    monkeypatch.setattr(
        schema_upgrade_module,
        "CURRENT_SCHEMA_REVISION",
        "schema_v2",
    )

    schema_upgrade_module.validate_schema_upgrade_artifacts()

    raw = migration.sql_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\x00" not in raw
    assert raw.decode("utf-8").count(SCHEMA_COMMENTS_PLACEHOLDER) == 1


@pytest.mark.parametrize(
    "revision",
    [
        pytest.param("schema_v0", id="zero"),
        pytest.param("schema_v01", id="leading-zero"),
        pytest.param("schema-v2", id="wrong-separator"),
        pytest.param("schema_v2_extra", id="suffix"),
        pytest.param("schema_v" + ("1" * 25), id="longer-than-varchar-32"),
    ],
)
def test_schema_upgrade_registry_rejects_an_invalid_current_revision(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    revision: str,
) -> None:
    migration = _migration(
        tmp_path,
        source_revision="schema_v1",
        target_revision=revision,
        filename="invalid-revision.sql",
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", (migration,))
    monkeypatch.setattr(schema_upgrade_module, "CURRENT_SCHEMA_REVISION", revision)

    with pytest.raises(RuntimeError, match=r"schema_vN|VARCHAR\(32\)"):
        schema_upgrade_module.validate_schema_upgrade_artifacts()


@pytest.mark.parametrize(
    ("source_revision", "target_revision"),
    [
        pytest.param("schema_v01", "schema_v2", id="invalid-source"),
        pytest.param("schema_v1", "schema_v02", id="invalid-target"),
    ],
)
def test_schema_upgrade_registry_rejects_invalid_migration_revisions(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_revision: str,
    target_revision: str,
) -> None:
    migration = _migration(
        tmp_path,
        source_revision=source_revision,
        target_revision=target_revision,
        filename="invalid-migration-revision.sql",
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", (migration,))
    monkeypatch.setattr(schema_upgrade_module, "CURRENT_SCHEMA_REVISION", "schema_v2")

    with pytest.raises(RuntimeError, match=r"schema_vN|VARCHAR\(32\)"):
        schema_upgrade_module.validate_schema_upgrade_artifacts()


@pytest.mark.parametrize(
    ("source_revision", "target_revision"),
    [
        pytest.param("schema_v2", "schema_v1", id="backwards"),
        pytest.param("schema_v2", "schema_v2", id="same-revision"),
    ],
)
def test_schema_upgrade_registry_rejects_non_increasing_revisions(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_revision: str,
    target_revision: str,
) -> None:
    migration = _migration(
        tmp_path,
        source_revision=source_revision,
        target_revision=target_revision,
        filename="non-increasing-revision.sql",
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", (migration,))
    monkeypatch.setattr(
        schema_upgrade_module,
        "CURRENT_SCHEMA_REVISION",
        target_revision,
    )

    with pytest.raises(RuntimeError, match="increase strictly"):
        schema_upgrade_module.validate_schema_upgrade_artifacts()


@pytest.mark.parametrize(
    "edges",
    [
        pytest.param(
            (
                ("schema_v1", "schema_v2"),
                ("schema_v1", "schema_v3"),
            ),
            id="fork",
        ),
        pytest.param(
            (
                ("schema_v1", "schema_v2"),
                ("schema_v2", "schema_v1"),
            ),
            id="cycle",
        ),
    ],
)
def test_schema_upgrade_registry_rejects_forks_and_cycles(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    edges: tuple[tuple[str, str], ...],
) -> None:
    migrations = tuple(
        _migration(
            tmp_path,
            source_revision=source_revision,
            target_revision=target_revision,
            filename=f"migration-{index}.sql",
        )
        for index, (source_revision, target_revision) in enumerate(edges)
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", migrations)

    with pytest.raises(RuntimeError, match="migration|upgrade|schema"):
        schema_upgrade_module.validate_schema_upgrade_artifacts()


def test_future_schema_upgrade_sql_is_a_transaction_free_comment_template(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration = _migration(
        tmp_path,
        source_revision="schema_v1",
        target_revision="schema_v2",
        filename="schema_v1_to_schema_v2.sql",
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", (migration,))
    monkeypatch.setattr(
        schema_upgrade_module,
        "CURRENT_SCHEMA_REVISION",
        "schema_v2",
    )
    sql = migration.sql_path.read_text(encoding="utf-8")

    assert sql.count(SCHEMA_COMMENTS_PLACEHOLDER) == 1
    assert not re.search(
        r"^\s*(?:BEGIN|COMMIT|ROLLBACK)\s*;",
        sql,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    assert not re.search(
        r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+alembic_version\b",
        sql,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    assert not re.search(
        r"^\s*COMMENT\s+ON\s+(?:TABLE|COLUMN)\b",
        sql,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    schema_upgrade_module.validate_schema_upgrade_artifacts()


@pytest.mark.parametrize(
    ("payload", "error_pattern"),
    [
        pytest.param(b"\xff", "UTF-8|artifact|migration|upgrade", id="invalid-utf8"),
        pytest.param(
            b"BEGIN;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "transaction|artifact|migration|upgrade",
            id="transaction-boundary",
        ),
        pytest.param(
            b"END;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|transaction|artifact|migration|upgrade",
            id="transaction-end-alias",
        ),
        pytest.param(
            b"ABORT;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|transaction|artifact|migration|upgrade",
            id="transaction-abort-alias",
        ),
        pytest.param(
            b"COMMIT AND CHAIN;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|transaction|artifact|migration|upgrade",
            id="transaction-chain",
        ),
        pytest.param(
            b"UPDATE alembic_version SET version_num = 'schema_v2';\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "marker|artifact|migration|upgrade",
            id="marker-mutation",
        ),
        pytest.param(
            b"TRUNCATE alembic_version;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|marker|artifact|migration|upgrade",
            id="marker-truncate",
        ),
        pytest.param(
            b"UPDATE public.alembic_version SET version_num = 'schema_v2';\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|marker|artifact|migration|upgrade",
            id="qualified-marker-mutation",
        ),
        pytest.param(
            b"COMMENT ON TABLE users IS 'duplicate';\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "COMMENT|artifact|migration|upgrade",
            id="embedded-comment",
        ),
        pytest.param(
            b"CREATE SCHEMA shadow;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|artifact|migration|upgrade",
            id="schema-management",
        ),
        pytest.param(
            b"CREATE TABLE shadow.future_example (id INTEGER);\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|artifact|migration|upgrade",
            id="non-public-qualification",
        ),
        pytest.param(
            b'CREATE TABLE "shadow"."future_example" (id INTEGER);\n' + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|artifact|migration|upgrade",
            id="quoted-non-public-qualification",
        ),
        pytest.param(
            b"CREATE TABLE shadow/**/.future_example (id INTEGER);\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|artifact|migration|upgrade",
            id="block-comment-qualified-name-bypass",
        ),
        pytest.param(
            b'CREATE TABLE U&"shadow"."future_example" (id INTEGER);\n' + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|artifact|migration|upgrade",
            id="unicode-escaped-qualified-name-bypass",
        ),
        pytest.param(
            b"SET search_path = shadow;\n" + SCHEMA_COMMENTS_PLACEHOLDER.encode() + b"\n",
            "forbidden|artifact|migration|upgrade",
            id="search-path-mutation",
        ),
        pytest.param(
            b"ALTER TABLE users ADD COLUMN example INTEGER;\n",
            "placeholder|comment|artifact|migration|upgrade",
            id="missing-comment-placeholder",
        ),
    ],
)
def test_schema_upgrade_artifact_validation_fails_closed(
    schema_upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
    error_pattern: str,
) -> None:
    migration = _migration(
        tmp_path,
        source_revision="schema_v1",
        target_revision="schema_v2",
        filename="schema_v1_to_schema_v2.sql",
        payload=payload,
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", (migration,))

    with pytest.raises(RuntimeError, match=error_pattern):
        schema_upgrade_module.validate_schema_upgrade_artifacts()
