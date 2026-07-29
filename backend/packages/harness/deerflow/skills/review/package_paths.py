"""Review-only helpers for paths under eval fixture directories."""

from pathlib import PurePosixPath


def is_eval_fixture_path(path: str | PurePosixPath) -> bool:
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    for index, part in enumerate(parts[:-1]):
        if part == "evals" and len(parts) > index + 2:
            return parts[index + 1] == "fixtures"
    return False


def is_eval_fixture_skill_md(path: str | PurePosixPath) -> bool:
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    return bool(parts) and parts[-1] == "SKILL.md" and is_eval_fixture_path(PurePosixPath(*parts[:-1]))
