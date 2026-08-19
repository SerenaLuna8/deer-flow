import logging
from pathlib import Path

from .frontmatter import (
    parse_required_secrets_value,
    parse_secrets_autonomous_value,
    parse_skill_frontmatter_document,
)
from .types import SKILL_MD_FILE, SecretRequirement, Skill, SkillCategory

logger = logging.getLogger(__name__)


def parse_allowed_tools(raw: object, skill_file: Path) -> tuple[str, ...] | None:
    """Parse the optional allowed-tools frontmatter field.

    Returns None when the field is omitted. Returns a tuple when the field is a
    YAML sequence of strings, including an empty tuple for explicit no-tool
    skills. Raises ValueError for malformed values.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"allowed-tools in {skill_file} must be a list of strings")

    allowed_tools: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"allowed-tools in {skill_file} must contain only strings")
        tool_name = item.strip()
        if not tool_name:
            raise ValueError(f"allowed-tools in {skill_file} cannot contain empty tool names")
        allowed_tools.append(tool_name)
    return tuple(allowed_tools)


def parse_required_secrets(raw: object, skill_file: Path) -> tuple[SecretRequirement, ...]:
    """Strict compatibility wrapper around the canonical value projector."""

    if raw is None:
        return ()
    try:
        return parse_required_secrets_value(raw)
    except ValueError as exc:
        raise ValueError(f"{exc} in {skill_file}") from None


def parse_secrets_autonomous(raw: object, skill_file: Path) -> bool:
    """Strict compatibility wrapper for ``secrets-autonomous``."""

    if raw is None:
        return True
    try:
        return parse_secrets_autonomous_value(raw)
    except ValueError as exc:
        raise ValueError(f"{exc} in {skill_file}") from None


def parse_skill_file(skill_file: Path, category: SkillCategory, relative_path: Path | None = None) -> Skill | None:
    """Parse a SKILL.md file and extract metadata.

    Args:
        skill_file: Path to the SKILL.md file.
        category: Category of the skill.
        relative_path: Relative path from the category root to the skill
            directory.  Defaults to the skill directory name when omitted.

    Returns:
        Skill object if parsing succeeds, None otherwise.
    """
    if not skill_file.exists() or skill_file.name != SKILL_MD_FILE:
        return None

    try:
        content = skill_file.read_text(encoding="utf-8")

        document = parse_skill_frontmatter_document(content)
        if not document.valid:
            logger.error(
                "Invalid Skill frontmatter in %s (%s)",
                skill_file,
                ",".join(item.code for item in document.diagnostics),
            )
            return None
        metadata = document.frontmatter
        projection = document.projection
        assert metadata is not None and projection is not None

        # Extract required fields.  Both must be non-empty strings.
        name = metadata.get("name")
        description = metadata.get("description")

        if not name or not isinstance(name, str):
            return None
        if not description or not isinstance(description, str):
            return None

        # Normalise: strip surrounding whitespace that YAML may preserve.
        name = name.strip()
        description = description.strip()

        if not name or not description:
            return None

        license_text = metadata.get("license")
        if license_text is not None:
            license_text = str(license_text).strip() or None

        try:
            allowed_tools = parse_allowed_tools(metadata.get("allowed-tools"), skill_file)
        except ValueError as exc:
            logger.error("Invalid allowed-tools in %s: %s", skill_file, exc)
            return None

        return Skill(
            name=name,
            description=description,
            license=license_text,
            skill_dir=skill_file.parent,
            skill_file=skill_file,
            relative_path=relative_path or Path(skill_file.parent.name),
            category=category,
            allowed_tools=allowed_tools,
            enabled=True,  # Actual state comes from the extensions config file.
            required_secrets=projection.required_secrets,
            secrets_autonomous=projection.secrets_autonomous,
        )

    except Exception:
        logger.exception("Unexpected error parsing skill file %s", skill_file)
        return None
