from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.private_work.model import RunAssetVersionRow
from deerflow.persistence.run.model import RunRow

_TEST_VERSION_NAMESPACE = uuid.UUID("f95cd779-dfe0-4ebf-9971-2979e667f7cd")


def _agent_asset_id(run: RunRow) -> uuid.UUID:
    try:
        return uuid.UUID(str(run.assistant_id))
    except (AttributeError, TypeError, ValueError):
        return uuid.uuid5(_TEST_VERSION_NAMESPACE, f"asset:{run.run_id}")


async def begin_test_run_closure(
    session: AsyncSession,
    run: RunRow,
) -> None:
    """Persist one explicit unsealed test Run and open its exact assembly."""

    run.asset_closure_sealed = False
    session.add(run)
    await session.flush()
    await session.scalar(
        select(
            func.set_config(
                "deerflow.run_asset_closure_assembly",
                run.run_id,
                True,
            )
        )
    )


async def seal_test_run_closure(
    session: AsyncSession,
    run: RunRow,
) -> None:
    """Flush test closure rows and make the exact Run executable."""

    await session.flush()
    run.asset_closure_sealed = True
    await session.flush()


def add_legacy_test_run_asset(
    session: AsyncSession,
    run: RunRow,
    *,
    asset_kind: str,
    dependency_order: int,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    payload_checksum: str,
    catalog_generation: int = 0,
    asset_scope: str = "project",
    payload: dict[str, object] | None = None,
) -> RunAssetVersionRow:
    snapshot_json: dict[str, object] = {
        "schema_version": 3,
        "kind": asset_kind,
        "scope": asset_scope,
        "asset_id": str(asset_id),
        "version_id": str(version_id),
        "checksum": payload_checksum,
        "catalog_generation": catalog_generation,
        "dependency_version_ids": [],
        asset_kind: {} if payload is None else payload,
    }
    row = RunAssetVersionRow(
        project_id=run.project_id,
        owner_user_id=run.owner_user_id,
        thread_id=run.thread_id,
        run_id=run.run_id,
        asset_kind=asset_kind,
        dependency_order=dependency_order,
        asset_scope=asset_scope,
        asset_id=asset_id,
        version_id=version_id,
        payload_checksum=payload_checksum,
        catalog_generation=catalog_generation,
        snapshot_schema_version=3,
        snapshot_json=snapshot_json,
    )
    session.add(row)
    return row


async def add_sealed_test_run(
    session: AsyncSession,
    run: RunRow,
) -> RunRow:
    """Persist a test Run with one explicit canonical legacy Agent parent."""

    await begin_test_run_closure(session, run)
    asset_id = _agent_asset_id(run)
    version_id = uuid.uuid5(_TEST_VERSION_NAMESPACE, f"version:{run.run_id}")
    checksum = hashlib.sha256(f"test-run-closure:{run.run_id}".encode()).hexdigest()
    add_legacy_test_run_asset(
        session,
        run,
        asset_kind="agent",
        dependency_order=0,
        asset_id=asset_id,
        version_id=version_id,
        payload_checksum=checksum,
    )
    await seal_test_run_closure(session, run)
    return run
