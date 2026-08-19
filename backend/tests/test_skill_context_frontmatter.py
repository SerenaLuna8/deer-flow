"""Skill read metadata must use the canonical SKILL.md parser."""

from __future__ import annotations

from deerflow.agents.middlewares.skill_context import (
    build_skill_entry_metadata_from_read,
)


def _description(content: str) -> str:
    metadata = build_skill_entry_metadata_from_read(
        "/mnt/skills/demo/SKILL.md",
        content,
        skills_root="/mnt/skills",
    )
    assert metadata is not None
    return metadata["description"]


def test_duplicate_description_key_fails_closed() -> None:
    content = """---
name: demo
description: first description
description: silently replaced description
---
# Demo
"""

    assert _description(content) == ""


def test_yaml_alias_description_fails_closed() -> None:
    content = """---
name: demo
shared: &shared alias description
description: *shared
---
# Demo
"""

    assert _description(content) == ""


def test_crlf_and_lf_documents_have_identical_description_semantics() -> None:
    lf = """---
name: demo
description: >
  first line
  second line
---
# Demo
"""
    crlf = lf.replace("\n", "\r\n")

    assert _description(lf) == "first line second line"
    assert _description(crlf) == _description(lf)


def test_invalid_managed_frontmatter_suppresses_otherwise_valid_description() -> None:
    content = """---
name: demo
description: should not survive invalid canonical frontmatter
required-secrets:
  - bad-name
---
# Demo
"""

    assert _description(content) == ""
