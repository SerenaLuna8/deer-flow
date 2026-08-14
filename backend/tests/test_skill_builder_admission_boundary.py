"""Import and identity contracts for Skill Builder Run admission."""

from __future__ import annotations

import inspect
from importlib import import_module
from types import ModuleType


def _module_origin(value: object) -> str | None:
    if isinstance(value, ModuleType):
        return value.__name__
    return getattr(value, "__module__", None)


def test_skill_builder_admission_contract_and_compatibility_identities() -> None:
    contract = import_module(
        "app.shared_assets.skill_builder_admission_contract",
    )
    facade = import_module("app.shared_assets.skill_builder_run_admission")
    concrete = import_module("app.private_work.skill_builder_run_admission")
    design_service = import_module("app.shared_assets.skill_design_service")
    router = import_module("app.gateway.routers.project_skill_builder")

    assert facade.SkillBuilderRunAdmission is contract.SkillBuilderRunAdmission
    assert facade.SkillBuilderRunAdmissionPort is contract.SkillBuilderRunAdmissionPort
    assert facade.SkillBuilderRunAdmissionService is concrete.SkillBuilderRunAdmissionService
    assert design_service.SkillBuilderRunAdmission is contract.SkillBuilderRunAdmission
    assert design_service.SkillBuilderRunAdmissionPort is contract.SkillBuilderRunAdmissionPort
    assert "SkillBuilderRunAdmissionService" not in vars(design_service)
    assert router.SkillBuilderRunAdmission is contract.SkillBuilderRunAdmission
    assert router.SkillBuilderRunAdmissionService is concrete.SkillBuilderRunAdmissionService


def test_skill_builder_admission_contract_has_no_private_work_dependency() -> None:
    contract = import_module(
        "app.shared_assets.skill_builder_admission_contract",
    )
    concrete = import_module("app.private_work.skill_builder_run_admission")
    design_service = import_module("app.shared_assets.skill_design_service")

    assert not any((origin or "").startswith("app.private_work.") for origin in (_module_origin(value) for value in vars(contract).values()))
    assert concrete.__name__ not in {_module_origin(value) for value in vars(design_service).values()}
    assert {name for name, value in vars(contract.SkillBuilderRunAdmissionPort).items() if not name.startswith("_") and inspect.isfunction(value)} == {"admit_in_session"}
    assert inspect.signature(
        contract.SkillBuilderRunAdmissionPort.admit_in_session,
    ) == inspect.signature(concrete.SkillBuilderRunAdmissionService.admit_in_session)
