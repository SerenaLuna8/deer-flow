from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

DOCTOR = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"


def _load_doctor() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "actweave_test_doctor_postgres",
        DOCTOR,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def doctor(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load_doctor()
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:secret@localhost/actweave")
    return module


def _result(schema_state: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "healthy": False,
        "schema_state": schema_state,
        "pg_trgm_installed": True,
        "vector_installed": True,
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize(
    ("schema_state", "expected_fix", "forbidden_fix"),
    [
        ("upgrade_required", "make upgrade-db", "make setup-db"),
        ("recreate_required", "make setup-db", None),
        ("uninitialized", "make setup-db", "make upgrade-db"),
        ("unavailable", "DATABASE_URL", "make upgrade-db"),
    ],
)
def test_doctor_postgres_uses_schema_state_specific_recovery(
    doctor: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    schema_state: str,
    expected_fix: str,
    forbidden_fix: str | None,
) -> None:
    monkeypatch.setattr(
        doctor,
        "_run_postgres_check",
        lambda *_args: _result(schema_state),
    )

    result = doctor.check_postgres(Path("."))

    assert result.status == "fail"
    assert result.fix is not None
    assert expected_fix in result.fix
    if forbidden_fix is not None:
        assert forbidden_fix not in result.fix


@pytest.mark.parametrize(
    ("overrides", "expected_extensions"),
    [
        ({"pg_trgm_installed": False}, "pg_trgm"),
        ({"vector_installed": False}, "vector"),
        (
            {"pg_trgm_installed": False, "vector_installed": False},
            "pg_trgm、vector",
        ),
    ],
)
def test_doctor_postgres_ready_schema_reports_missing_extensions_without_upgrade(
    doctor: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    expected_extensions: str,
) -> None:
    monkeypatch.setattr(
        doctor,
        "_run_postgres_check",
        lambda *_args: _result("ready", **overrides),
    )

    result = doctor.check_postgres(Path("."))

    assert result.status == "fail"
    assert expected_extensions in result.detail
    assert result.fix is not None
    assert expected_extensions in result.fix
    assert "make upgrade-db" not in result.fix
