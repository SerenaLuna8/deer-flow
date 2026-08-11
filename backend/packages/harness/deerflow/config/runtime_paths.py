"""Runtime path resolution for standalone harness usage."""

import os
from collections.abc import Mapping
from pathlib import Path

ACT_WEAVE_PROJECT_ROOT_ENV = "ACT_WEAVE_PROJECT_ROOT"
DEER_FLOW_PROJECT_ROOT_ENV = "DEER_FLOW_PROJECT_ROOT"
ACT_WEAVE_HOME_ENV = "ACT_WEAVE_HOME"
DEER_FLOW_HOME_ENV = "DEER_FLOW_HOME"
ACT_WEAVE_CONFIG_PATH_ENV = "ACT_WEAVE_CONFIG_PATH"
DEER_FLOW_CONFIG_PATH_ENV = "DEER_FLOW_CONFIG_PATH"
ACT_WEAVE_SKILLS_PATH_ENV = "ACT_WEAVE_SKILLS_PATH"
DEER_FLOW_SKILLS_PATH_ENV = "DEER_FLOW_SKILLS_PATH"


class RuntimePathConflict(ValueError):
    """Two environment names for one runtime path disagree."""


def _configured_value(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name)
    return value if value else None


def resolve_environment_path(
    canonical_name: str,
    legacy_name: str,
    *,
    environment: Mapping[str, str] | None = None,
    default: str | os.PathLike[str] | None = None,
    base: Path | None = None,
) -> Path | None:
    """Resolve a new/legacy local path alias pair and reject disagreement."""
    values = os.environ if environment is None else environment
    canonical_value = _configured_value(values, canonical_name)
    legacy_value = _configured_value(values, legacy_name)
    canonical_path = resolve_path(canonical_value, base=base or Path.cwd()) if canonical_value else None
    legacy_path = resolve_path(legacy_value, base=base or Path.cwd()) if legacy_value else None
    if canonical_path is not None and legacy_path is not None and canonical_path != legacy_path:
        raise RuntimePathConflict(f"{canonical_name} resolves to '{canonical_path}', but {legacy_name} resolves to '{legacy_path}'; refusing conflicting local runtime paths")
    if canonical_path is not None:
        return canonical_path
    if legacy_path is not None:
        return legacy_path
    if default is None:
        return None
    return resolve_path(default, base=base or Path.cwd())


def project_root() -> Path:
    """Return the caller project root for runtime-owned files."""
    root = resolve_environment_path(
        ACT_WEAVE_PROJECT_ROOT_ENV,
        DEER_FLOW_PROJECT_ROOT_ENV,
        default=Path.cwd(),
        base=Path.cwd(),
    )
    assert root is not None
    if os.getenv(ACT_WEAVE_PROJECT_ROOT_ENV) or os.getenv(DEER_FLOW_PROJECT_ROOT_ENV):
        if not root.exists():
            raise ValueError(f"{ACT_WEAVE_PROJECT_ROOT_ENV}/{DEER_FLOW_PROJECT_ROOT_ENV} resolves to '{root}', but that path does not exist.")
        if not root.is_dir():
            raise ValueError(f"{ACT_WEAVE_PROJECT_ROOT_ENV}/{DEER_FLOW_PROJECT_ROOT_ENV} resolves to '{root}', but that path is not a directory.")
    return root


def runtime_home() -> Path:
    """Return the writable ActWeave state directory."""
    home = resolve_environment_path(
        ACT_WEAVE_HOME_ENV,
        DEER_FLOW_HOME_ENV,
        base=Path.cwd(),
    )
    if home is not None:
        return home
    return project_root() / ".act-weave"


def resolve_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """Resolve absolute paths as-is and relative paths against the project root."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or project_root()) / path
    return path.resolve()


def existing_project_file(names: tuple[str, ...]) -> Path | None:
    """Return the first existing named file under the project root."""
    root = project_root()
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None
