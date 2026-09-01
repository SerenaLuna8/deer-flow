from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

DOCTOR = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"


def _load_doctor() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "actweave_test_doctor_model_catalog",
        DOCTOR,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_doctor_model_catalog_query_matches_schema_v1(
    migrated_postgres_database_url: str,
) -> None:
    result = await asyncio.to_thread(
        _load_doctor()._run_model_catalog_query,
        migrated_postgres_database_url,
    )

    assert result == {"table_exists": True, "active_count": 0}
