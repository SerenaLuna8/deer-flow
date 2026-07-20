from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_acceptance.contracts import (
    discover_scoped_surface,
    load_isolation_matrix,
    validate_evidence_selectors,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MATRIX_PATH = _REPO_ROOT / "contracts" / "m8_isolation_matrix.json"


@pytest.fixture(scope="module")
def matrix():
    return load_isolation_matrix(_MATRIX_PATH)


def test_matrix_covers_every_frozen_dimension(matrix) -> None:
    assert matrix.cases
    assert matrix.uncovered_dimensions() == ()
    assert len(matrix.case_ids) == len(matrix.cases)


def test_every_selector_exists_without_static_skip_or_xfail(matrix) -> None:
    report = validate_evidence_selectors(matrix, _REPO_ROOT)
    assert report.missing == ()
    assert report.skipped == ()
    assert report.xfailed == ()
    assert report.pytest_count > 0
    assert report.playwright_count > 0


def test_final_scoped_surface_has_matrix_authority(matrix) -> None:
    discovered = discover_scoped_surface(_REPO_ROOT)
    assert discovered
    assert matrix.surface_manifest.count == len(discovered)
    assert matrix.surface_manifest.sha256 == matrix.discovered_surface_digest(discovered)
    assert matrix.unmapped_surface(discovered) == ()
    assert matrix.orphaned_surface_cases(discovered) == ()


def test_discovered_surface_classification_is_closed() -> None:
    discovered = discover_scoped_surface(_REPO_ROOT)
    assert {surface.operation for surface in discovered} <= set(load_isolation_matrix(_MATRIX_PATH).dimensions.operations)
    assert {surface.resource_family for surface in discovered} <= set(load_isolation_matrix(_MATRIX_PATH).dimensions.resource_families)
    assert {surface.layer for surface in discovered} <= set(load_isolation_matrix(_MATRIX_PATH).dimensions.layers)


def test_denial_cases_require_zero_database_delta_and_database_evidence(matrix) -> None:
    denial_cases = [case for case in matrix.cases if case.expected_status >= 400]
    assert denial_cases
    for case in denial_cases:
        assert case.expected_db_delta == 0, case.case_id
        assert "database" in case.layers, case.case_id
        assert any(selector.startswith("pytest::") and "postgres.py::" in selector for selector in case.evidence_selectors), case.case_id


def test_matrix_covers_release_relevant_denial_statuses(matrix) -> None:
    assert {401, 403, 404, 409, 422, 429, 503} <= {case.expected_status for case in matrix.cases}


def test_selector_paths_cannot_escape_the_test_roots(tmp_path: Path) -> None:
    payload = json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["evidence_selectors"] = [
        "pytest::tests/../../outside_postgres.py::test_escape",
    ]
    malicious = tmp_path / "matrix.json"
    malicious.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid matrix evidence selector"):
        load_isolation_matrix(malicious)
