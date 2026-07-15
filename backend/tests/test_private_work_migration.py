from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from scripts.migrate_private_work import (
    LegacyOwnerInventory,
    PrivateWorkInventory,
    PrivateWorkMigrationError,
    build_migration_plan,
    load_owner_map,
    render_inventory,
)


def _inventory(*owners: uuid.UUID) -> PrivateWorkInventory:
    return PrivateWorkInventory(
        source_fingerprint="a" * 64,
        owners=tuple(
            LegacyOwnerInventory(
                owner_user_id=str(owner),
                thread_count=1,
                run_count=1,
                event_count=1,
                feedback_count=1,
            )
            for owner in owners
        ),
        checkpoint_count=0,
        filesystem_source_count=0,
    )


def test_owner_map_requires_explicit_project_for_every_legacy_owner(tmp_path: Path) -> None:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    project_a = uuid.uuid4()
    owner_map_path = tmp_path / "owners.json"
    owner_map_path.write_text(json.dumps({str(owner_a): str(project_a)}), encoding="utf-8")

    owner_map = load_owner_map(owner_map_path)

    with pytest.raises(PrivateWorkMigrationError, match="owner map incomplete"):
        build_migration_plan(_inventory(owner_a, owner_b), owner_map)


def test_owner_map_rejects_non_uuid_coordinates_without_echoing_input(tmp_path: Path) -> None:
    owner_map_path = tmp_path / "owners.json"
    owner_map_path.write_text('{"private@example.invalid":"not-a-project"}', encoding="utf-8")

    with pytest.raises(PrivateWorkMigrationError, match="owner map is invalid") as caught:
        load_owner_map(owner_map_path)

    assert "private@example.invalid" not in str(caught.value)
    assert "not-a-project" not in str(caught.value)


def test_inventory_output_is_stable_and_contains_only_redacted_counts() -> None:
    owner = uuid.uuid4()
    inventory = _inventory(owner)

    first = render_inventory(inventory)
    second = render_inventory(inventory)

    assert first == second
    assert str(owner) not in first
    assert "source_fingerprint" not in first
    assert json.loads(first) == {
        "counts": {
            "checkpoints": 0,
            "filesystem_sources": 0,
            "legacy_owners": 1,
            "run_events": 1,
            "runs": 1,
            "feedback": 1,
            "threads": 1,
        },
        "source_key_hash": "a" * 12,
    }


def test_plan_rejects_unsupported_filesystem_sources_before_execute() -> None:
    owner = uuid.uuid4()
    project = uuid.uuid4()
    inventory = PrivateWorkInventory(
        source_fingerprint="b" * 64,
        owners=(LegacyOwnerInventory(str(owner), 0, 0, 0, 0),),
        checkpoint_count=0,
        filesystem_source_count=1,
    )

    with pytest.raises(PrivateWorkMigrationError, match="unsupported legacy source"):
        build_migration_plan(inventory, {str(owner): project})
