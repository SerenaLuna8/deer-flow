from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.shared_assets.errors import SkillSecretConfigurationInvalid

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_SKILL_SECRETS = 256
MAX_SKILL_SECRET_NAME_LENGTH = 255


@dataclass(frozen=True, slots=True)
class SkillSecretDeclaration:
    name: str
    target_env: str
    optional: bool


def parse_skill_secret_declarations(
    raw: object,
    *,
    request_id: str,
) -> tuple[SkillSecretDeclaration, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_SKILL_SECRETS:
        raise SkillSecretConfigurationInvalid(request_id)
    result: list[SkillSecretDeclaration] = []
    seen_names: set[str] = set()
    seen_targets: set[str] = set()
    for item in raw:
        if (
            not isinstance(item, Mapping)
            or set(item) - {"name", "target_env", "optional"}
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("target_env", item.get("name")), str)
            or not isinstance(item.get("optional", False), bool)
        ):
            raise SkillSecretConfigurationInvalid(request_id)
        name = item["name"]
        target_env = item.get("target_env", name)
        if len(name) > MAX_SKILL_SECRET_NAME_LENGTH or _ENV_NAME.fullmatch(name) is None or name in seen_names or len(target_env) > MAX_SKILL_SECRET_NAME_LENGTH or _ENV_NAME.fullmatch(target_env) is None or target_env in seen_targets:
            raise SkillSecretConfigurationInvalid(request_id)
        seen_names.add(name)
        seen_targets.add(target_env)
        result.append(
            SkillSecretDeclaration(
                name=name,
                target_env=target_env,
                optional=item.get("optional", False),
            )
        )
    return tuple(result)


def parse_skill_secret_requirements(
    raw: object,
    *,
    request_id: str,
) -> tuple[tuple[str, bool], ...]:
    return tuple((item.name, item.optional) for item in parse_skill_secret_declarations(raw, request_id=request_id))


def normalize_skill_secret_values(
    values: Mapping[str, str] | Sequence[tuple[str, str]],
    *,
    declared_names: frozenset[str],
    request_id: str,
) -> dict[str, str]:
    try:
        items = values.items() if isinstance(values, Mapping) else values
        normalized: dict[str, str] = {}
        for name, value in items:
            if not isinstance(name, str) or name not in declared_names or name in normalized or not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > 64 * 1024:
                raise ValueError
            if value:
                normalized[name] = value
    except (AttributeError, RecursionError, TypeError, ValueError):
        raise SkillSecretConfigurationInvalid(request_id) from None
    return normalized


__all__ = [
    "MAX_SKILL_SECRETS",
    "SkillSecretDeclaration",
    "normalize_skill_secret_values",
    "parse_skill_secret_declarations",
    "parse_skill_secret_requirements",
]
