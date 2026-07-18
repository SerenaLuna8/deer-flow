from __future__ import annotations

import inspect
from pathlib import Path

from deerflow.agents.lead_agent import prompt
from deerflow.agents.middlewares.skill_activation_middleware import (
    SkillActivationMiddleware,
)
from deerflow.skills.types import Skill, SkillCategory


def _runtime_skill(root: Path) -> Skill:
    skill_dir = root / "exact-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: exact-skill\ndescription: exact\n---\n", encoding="utf-8")
    return Skill(
        name="exact-skill",
        description="exact",
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_file,
        relative_path=Path("exact-skill"),
        category=SkillCategory.CUSTOM,
        enabled=True,
        runtime_read_only=True,
    )


def test_prompt_module_has_no_file_backed_skill_discovery() -> None:
    source = inspect.getsource(prompt)
    assert "get_or_new_skill_storage" not in source
    assert "get_or_new_user_skill_storage" not in source
    assert prompt.get_enabled_skills_for_config() == []


def test_slash_activation_reads_only_exact_runtime_skill(tmp_path: Path) -> None:
    skill = _runtime_skill(tmp_path)
    middleware = SkillActivationMiddleware(
        runtime_skills=(skill,),
        runtime_skills_root=tmp_path,
        runtime_skills_container_path="/mnt/skills",
    )

    resolution = middleware._resolve_activation("/exact-skill do it")

    assert resolution is not None
    assert resolution.activation is not None
    assert resolution.activation.skill_name == "exact-skill"
    assert resolution.activation.editable is False


def test_slash_activation_without_run_snapshot_exposes_nothing() -> None:
    middleware = SkillActivationMiddleware()
    resolution = middleware._resolve_activation("/unknown do it")
    assert resolution is not None
    assert resolution.failure_message == "Skill `/unknown` is not installed."


def test_subagent_executor_has_no_storage_lookup() -> None:
    executor_path = Path(__file__).parents[1] / "packages/harness/deerflow/subagents/executor.py"
    source = executor_path.read_text(encoding="utf-8")
    assert "get_or_new_skill_storage" not in source
    assert "self._runtime_skills" in source
