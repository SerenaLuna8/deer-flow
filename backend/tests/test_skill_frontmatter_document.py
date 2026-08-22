from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deerflow.skills.frontmatter import (
    MAX_SKILL_FRONTMATTER_BYTES,
    MAX_SKILL_FRONTMATTER_DEPTH,
    MAX_SKILL_FRONTMATTER_NODES,
    MAX_SKILL_SECRET_REQUIREMENTS,
    SkillFrontmatterPatchRejected,
    SkillFrontmatterSourceMismatch,
    parse_skill_frontmatter_document,
)
from deerflow.skills.frontmatter_patch import patch_skill_frontmatter_document
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.review.analyzer import analyze_skill_package
from deerflow.skills.types import SecretRequirement, SkillCategory
from deerflow.skills.validation import _validate_skill_frontmatter

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _document(frontmatter: str, body: str = "# Instructions\n") -> str:
    return f"---\n{frontmatter}\n---\n{body}"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _diagnostic_codes(content: str) -> set[str]:
    return {item.code for item in parse_skill_frontmatter_document(content).diagnostics}


def test_parse_projects_mapping_requirements_and_explicit_autonomous() -> None:
    content = _document(
        """name: example-skill
description: Example
required-secrets:
  - name: \"provider_key\"
    target_env: \"OPENAI_API_KEY\"
    optional: false
  - name: \"secondary_token\"
    target_env: \"SECONDARY_TOKEN\"
    optional: true
secrets-autonomous: false"""
    )

    result = parse_skill_frontmatter_document(content)

    assert result.valid is True
    assert result.patchable is True
    assert result.source_sha256 == _sha256(content)
    assert result.body == "# Instructions\n"
    assert result.frontmatter == {
        "name": "example-skill",
        "description": "Example",
        "required-secrets": [
            {
                "name": "provider_key",
                "target_env": "OPENAI_API_KEY",
                "optional": False,
            },
            {
                "name": "secondary_token",
                "target_env": "SECONDARY_TOKEN",
                "optional": True,
            },
        ],
        "secrets-autonomous": False,
    }
    assert result.projection is not None
    assert result.projection.required_secrets == (
        SecretRequirement(
            name="provider_key",
            target_env="OPENAI_API_KEY",
            optional=False,
        ),
        SecretRequirement(
            name="secondary_token",
            target_env="SECONDARY_TOKEN",
            optional=True,
        ),
    )
    assert result.projection.secrets_autonomous is False
    assert result.projection.secrets_autonomous_explicit is True
    assert result.projection.shorthand_count == 0
    assert result.diagnostics == ()


def test_parse_accepts_historical_shorthand_without_normalizing_source() -> None:
    content = _document(
        """name: example-skill
description: Example
required-secrets:
  - API_TOKEN
  - OPTIONAL_TOKEN"""
    )

    result = parse_skill_frontmatter_document(content)

    assert result.valid is True
    assert result.patchable is True
    assert result.projection is not None
    assert result.projection.required_secrets == (
        SecretRequirement(name="API_TOKEN", optional=False),
        SecretRequirement(name="OPTIONAL_TOKEN", optional=False),
    )
    assert result.projection.secrets_autonomous is True
    assert result.projection.secrets_autonomous_explicit is False
    assert result.projection.shorthand_count == 2
    assert content == _document(
        """name: example-skill
description: Example
required-secrets:
  - API_TOKEN
  - OPTIONAL_TOKEN"""
    )


@pytest.mark.parametrize(
    ("frontmatter", "expected_code", "expected_path"),
    [
        (
            "name: example\ndescription: Example\nrequired-secrets: API_TOKEN",
            "required_secrets_not_list",
            ("required-secrets",),
        ),
        (
            "name: example\ndescription: Example\nrequired-secrets:\n  - 123",
            "required_secret_invalid_item",
            ("required-secrets", 0),
        ),
        (
            "name: example\ndescription: Example\nrequired-secrets:\n  - name: API_TOKEN\n    other: true",
            "required_secret_unknown_field",
            ("required-secrets", 0),
        ),
        (
            "name: example\ndescription: Example\nrequired-secrets:\n  - optional: false",
            "required_secret_name_required",
            ("required-secrets", 0, "name"),
        ),
        (
            "name: example\ndescription: Example\nrequired-secrets:\n  - name: NOT-AN-ENV",
            "invalid_env_name",
            ("required-secrets", 0, "name"),
        ),
        (
            "name: example\ndescription: Example\nrequired-secrets:\n  - name: API_TOKEN\n    optional: 1",
            "required_secret_optional_not_boolean",
            ("required-secrets", 0, "optional"),
        ),
        (
            "name: example\ndescription: Example\nrequired-secrets:\n  - API_TOKEN\n  - name: API_TOKEN",
            "duplicate_env_name",
            ("required-secrets", 1, "name"),
        ),
        (
            'name: example\ndescription: Example\nsecrets-autonomous: "true"',
            "secrets_autonomous_not_boolean",
            ("secrets-autonomous",),
        ),
    ],
)
def test_parse_rejects_invalid_secret_declarations_with_structured_diagnostics(
    frontmatter: str,
    expected_code: str,
    expected_path: tuple[str | int, ...],
) -> None:
    result = parse_skill_frontmatter_document(_document(frontmatter))

    assert result.valid is False
    assert result.patchable is False
    assert result.projection is None
    diagnostic = next(item for item in result.diagnostics if item.code == expected_code)
    assert diagnostic.severity == "error"
    assert diagnostic.field_path == expected_path
    assert diagnostic.line is not None and diagnostic.line >= 2
    assert diagnostic.column is not None and diagnostic.column >= 1


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("# no frontmatter\n", "frontmatter_missing"),
        (_document("- not\n- a\n- mapping"), "frontmatter_not_mapping"),
        (_document("name: one\nname: two\ndescription: Example"), "duplicate_mapping_key"),
        (_document("name: example\ndescription: Example\nmetadata: &shared {kind: test}\ncompatibility: *shared"), "yaml_alias_not_allowed"),
        (_document("name: [unterminated\ndescription: Example"), "yaml_invalid"),
    ],
)
def test_parse_rejects_invalid_or_unsafe_yaml(content: str, expected_code: str) -> None:
    result = parse_skill_frontmatter_document(content)

    assert result.valid is False
    assert result.patchable is False
    assert result.projection is None
    assert expected_code in {item.code for item in result.diagnostics}


def test_parse_enforces_frontmatter_byte_limit() -> None:
    padding = "x" * MAX_SKILL_FRONTMATTER_BYTES
    content = _document(f"name: example\ndescription: Example\nmetadata: {padding}")

    result = parse_skill_frontmatter_document(content)

    assert result.valid is False
    assert _diagnostic_codes(content) == {"frontmatter_too_large"}


def test_parse_enforces_node_limit() -> None:
    # Each scalar contributes a node in addition to the surrounding mappings
    # and sequence, so this exceeds the public bound without relying on aliases.
    values = ", ".join("0" for _ in range(MAX_SKILL_FRONTMATTER_NODES))
    content = _document(f"name: example\ndescription: Example\nmetadata: [{values}]")

    result = parse_skill_frontmatter_document(content)

    assert result.valid is False
    assert "yaml_node_limit_exceeded" in _diagnostic_codes(content)


def test_parse_enforces_depth_limit() -> None:
    nested = "value"
    for _ in range(MAX_SKILL_FRONTMATTER_DEPTH + 1):
        nested = f"[{nested}]"
    content = _document(f"name: example\ndescription: Example\nmetadata: {nested}")

    result = parse_skill_frontmatter_document(content)

    assert result.valid is False
    assert "yaml_depth_limit_exceeded" in _diagnostic_codes(content)


def test_parse_and_patch_enforce_secret_requirement_limit() -> None:
    names = [f"TOKEN_{index}" for index in range(MAX_SKILL_SECRET_REQUIREMENTS + 1)]
    content = _document("name: example\ndescription: Example\nrequired-secrets:\n" + "\n".join(f"  - {name}" for name in names))

    assert _diagnostic_codes(content) == {"required_secrets_limit_exceeded"}

    valid_content = _document("name: example\ndescription: Example")
    with pytest.raises(SkillFrontmatterPatchRejected) as rejected:
        patch_skill_frontmatter_document(
            valid_content,
            expected_source_sha256=_sha256(valid_content),
            required_secrets=tuple(SecretRequirement(name) for name in names),
            secrets_autonomous=True,
        )
    assert {item.code for item in rejected.value.diagnostics} == {"required_secrets_limit_exceeded"}


def test_parse_preserves_historical_posix_name_longer_than_binding_column() -> None:
    name = "A" * 300
    content = _document(f"name: example\ndescription: Example\nrequired-secrets:\n  - name: {name}\n    optional: false")

    parsed = parse_skill_frontmatter_document(content)
    patched = patch_skill_frontmatter_document(
        content,
        expected_source_sha256=_sha256(content),
        required_secrets=(SecretRequirement(name),),
        secrets_autonomous=True,
    )

    assert parsed.valid is True
    assert parsed.projection is not None
    assert parsed.projection.required_secrets == (SecretRequirement(name),)
    assert patched.changed is False
    assert patched.content == content


def test_diagnostics_are_safe_and_do_not_echo_source_or_yaml_exception() -> None:
    marker = "DO_NOT_ECHO_THIS_VALUE"
    content = _document(f"name: example\ndescription: Example\nrequired-secrets:\n  - name: {marker}-INVALID")

    result = parse_skill_frontmatter_document(content)
    rendered = repr(result.diagnostics)

    assert marker not in rendered
    assert "SKILL.md" not in rendered
    assert "while parsing" not in rendered
    assert all("/" not in item.public_message for item in result.diagnostics)


def test_patch_noop_is_byte_exact_and_keeps_shorthand() -> None:
    content = _document(
        """# catalog comment
name: example
description: Example
required-secrets:
  - API_TOKEN
secrets-autonomous: true"""
    )

    result = patch_skill_frontmatter_document(
        content,
        expected_source_sha256=_sha256(content),
        required_secrets=(SecretRequirement("API_TOKEN"),),
        secrets_autonomous=True,
    )

    assert result.content == content
    assert result.changed is False
    assert result.changed_fields == ()
    assert result.source_sha256 == result.result_sha256 == _sha256(content)
    assert result.projection.shorthand_count == 1


def test_patch_updates_only_managed_fields_and_is_idempotent() -> None:
    content = _document(
        """# catalog comment
name: example
description: >-
  A folded description that must stay byte-exact.
required-secrets:
  - API_TOKEN
metadata: {owner: team-a} # unmanaged inline comment
secrets-autonomous: true""",
        body="\n# Instructions\n\nKeep this body byte-exact.\n",
    )

    first = patch_skill_frontmatter_document(
        content,
        expected_source_sha256=_sha256(content),
        required_secrets=(
            SecretRequirement("API_TOKEN"),
            SecretRequirement("OPTIONAL_TOKEN", optional=True),
        ),
        secrets_autonomous=False,
    )
    second = patch_skill_frontmatter_document(
        first.content,
        expected_source_sha256=first.result_sha256,
        required_secrets=(
            SecretRequirement("API_TOKEN"),
            SecretRequirement("OPTIONAL_TOKEN", optional=True),
        ),
        secrets_autonomous=False,
    )

    assert first.changed is True
    assert first.changed_fields == ("required-secrets", "secrets-autonomous")
    assert ('  - name: "API_TOKEN"\n    target_env: "API_TOKEN"\n    optional: false') in first.content
    assert ('  - name: "OPTIONAL_TOKEN"\n    target_env: "OPTIONAL_TOKEN"\n    optional: true') in first.content
    assert "metadata: {owner: team-a} # unmanaged inline comment" in first.content
    assert first.content.endswith("\n# Instructions\n\nKeep this body byte-exact.\n")
    assert second.content == first.content
    assert second.changed is False
    assert second.changed_fields == ()


def test_patch_quotes_yaml_11_boolean_and_null_like_environment_names() -> None:
    content = _document("name: example\ndescription: Example")
    names = ("YES", "NO", "ON", "OFF", "NULL")

    result = patch_skill_frontmatter_document(
        content,
        expected_source_sha256=_sha256(content),
        required_secrets=tuple(SecretRequirement(name) for name in names),
        secrets_autonomous=True,
    )

    for name in names:
        assert f'name: "{name}"' in result.content
    assert result.projection.required_secrets == tuple(SecretRequirement(name) for name in names)


def test_patch_preserves_crlf_and_unmanaged_comments() -> None:
    content = "---\r\n# keep me\r\nname: example\r\ndescription: Example\r\nmetadata: value # keep inline\r\n---\r\n\r\n# Body\r\n"

    result = patch_skill_frontmatter_document(
        content,
        expected_source_sha256=_sha256(content),
        required_secrets=(SecretRequirement("API_TOKEN"),),
        secrets_autonomous=False,
    )

    assert "\n" not in result.content.replace("\r\n", "")
    assert "# keep me\r\n" in result.content
    assert "metadata: value # keep inline\r\n" in result.content
    assert result.content.endswith("\r\n\r\n# Body\r\n")


@pytest.mark.parametrize(
    "managed_source",
    [
        "required-secrets: # managed comment\n  - API_TOKEN",
        "required-secrets:\n  # managed comment\n  - API_TOKEN",
        "required-secrets:\n  - API_TOKEN # managed comment",
        "secrets-autonomous: true # managed comment",
    ],
)
def test_parse_marks_managed_comments_as_valid_but_not_patchable(managed_source: str) -> None:
    content = _document(f"name: example\ndescription: Example\n{managed_source}")

    parsed = parse_skill_frontmatter_document(content)

    assert parsed.valid is True
    assert parsed.patchable is False
    assert parsed.projection is not None
    diagnostic = next(item for item in parsed.diagnostics if item.code == "managed_comments_unsupported")
    assert diagnostic.severity == "warning"

    with pytest.raises(SkillFrontmatterPatchRejected) as rejected:
        patch_skill_frontmatter_document(
            content,
            expected_source_sha256=_sha256(content),
            required_secrets=(SecretRequirement("OTHER_TOKEN"),),
            secrets_autonomous=True,
        )
    assert {item.code for item in rejected.value.diagnostics} == {"managed_comments_unsupported"}


def test_patch_rejects_stale_source_hash_without_exposing_content() -> None:
    content = _document("name: example\ndescription: Example")

    with pytest.raises(SkillFrontmatterSourceMismatch) as mismatch:
        patch_skill_frontmatter_document(
            content,
            expected_source_sha256="0" * 64,
            required_secrets=(),
            secrets_autonomous=True,
        )

    assert mismatch.value.expected_source_sha256 == "0" * 64
    assert mismatch.value.actual_source_sha256 == _sha256(content)
    assert content not in str(mismatch.value)


def test_patch_rejects_invalid_requested_projection() -> None:
    content = _document("name: example\ndescription: Example")

    with pytest.raises(SkillFrontmatterPatchRejected) as rejected:
        patch_skill_frontmatter_document(
            content,
            expected_source_sha256=_sha256(content),
            required_secrets=(SecretRequirement("INVALID-NAME"),),
            secrets_autonomous=True,
        )

    assert {item.code for item in rejected.value.diagnostics} == {"invalid_env_name"}


def test_patch_can_remove_requirements_without_leaving_an_extra_blank_line() -> None:
    content = _document("name: example\ndescription: Example\nrequired-secrets:\n  - API_TOKEN")

    result = patch_skill_frontmatter_document(
        content,
        expected_source_sha256=_sha256(content),
        required_secrets=(),
        secrets_autonomous=True,
    )

    assert result.content == _document("name: example\ndescription: Example")
    assert result.changed_fields == ("required-secrets",)


def test_packaged_system_skills_are_canonical_parser_compatible() -> None:
    skill_files = sorted((_REPOSITORY_ROOT / "skills" / "public").glob("*/SKILL.md"))
    assert skill_files

    results = {skill_file.relative_to(_REPOSITORY_ROOT).as_posix(): parse_skill_frontmatter_document(skill_file.read_text(encoding="utf-8")) for skill_file in skill_files}

    assert {path: result.diagnostics for path, result in results.items() if not result.valid} == {}


def test_parser_validation_and_review_share_canonical_secret_rejection(tmp_path: Path) -> None:
    content = _document("name: example\ndescription: Example\nrequired-secrets:\n  - name: INVALID-NAME")
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    assert parse_skill_file(skill_dir / "SKILL.md", SkillCategory.CUSTOM) is None
    valid, message, name = _validate_skill_frontmatter(skill_dir)
    assert valid is False
    assert message == "Environment variable names must use POSIX syntax"
    assert name is None

    facts = analyze_skill_package(
        {
            "subject": {"display_ref": "example", "source": "test", "category": "custom"},
            "files": [{"path": "SKILL.md", "kind": "text", "content": content}],
            "reader_errors": [],
            "truncated": False,
        }
    )
    finding = next(item for item in facts["findings"] if item["rule_id"] == "structure.invalid-frontmatter")
    assert finding["message"] == "Environment variable names must use POSIX syntax"
