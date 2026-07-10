"""Regression anchor: skill content reads must not block the event loop.

The gateway content route discovers the current user's visible skills, validates
the registered ``SKILL.md`` path, and reads its text. All of those synchronous
filesystem operations must remain inside the route's ``asyncio.to_thread``
offload; removing it makes the strict Blockbuster gate fail this test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gateway.routers import skills as skills_router
from deerflow.config.paths import Paths
from deerflow.skills.storage.user_scoped_skill_storage import UserScopedSkillStorage

pytestmark = pytest.mark.asyncio

_CONTENT = """---
name: loop-skill
description: Blocking IO regression fixture.
---

# Loop Skill
"""


def _seed_skill(skills_root: Path) -> None:
    skill_dir = skills_root / "public" / "loop-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_CONTENT, encoding="utf-8")


async def test_get_skill_content_does_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skills_root = tmp_path / "skills"
    await asyncio.to_thread(_seed_skill, skills_root)
    paths = await asyncio.to_thread(Paths, base_dir=tmp_path)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr("deerflow.config.paths._paths", None)
    storage = await asyncio.to_thread(UserScopedSkillStorage, "default", host_path=str(skills_root))
    monkeypatch.setattr(skills_router, "_get_user_skill_storage", lambda _config: storage)

    async def _allow_admin(_request, *, detail: str) -> None:
        return None

    monkeypatch.setattr(skills_router, "require_admin_user", _allow_admin)

    response = await skills_router.get_skill_content(
        "loop-skill",
        request=SimpleNamespace(),
        config=SimpleNamespace(),
    )

    assert response.content == _CONTENT
