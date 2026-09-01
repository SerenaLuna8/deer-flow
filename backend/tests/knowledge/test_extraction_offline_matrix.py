"""Runs every registered file/ETL route through the real OS sandbox."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _gate_module():
    path = Path(__file__).parents[2] / "scripts/check_extraction_runtime.py"
    assert path.exists(), "offline matrix gate is not implemented"
    spec = importlib.util.spec_from_file_location("extraction_runtime_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_complete_registry_matrix_in_os_sandbox(tmp_path):
    gate = _gate_module()
    report = await gate.run_matrix(tmp_path)
    assert report["counts"]["failed"] == 0, [(row["mode"], row["extension"], row.get("reason_code")) for row in report["matrix"] if row["result"] != "passed"]
    assert report["counts"]["skipped"] == 0
    assert report["counts"]["passed"] == len(gate.matrix_routes())
    assert report["counts"]["passed"] >= 29
    assert all(row["source_spans"] for row in report["matrix"])
    assert "page_content" not in str(report)
