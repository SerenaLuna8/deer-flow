from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _script_directory() -> ScriptDirectory:
    config = AlembicConfig()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config)


@dataclass(frozen=True, slots=True)
class RevisionAncestry:
    ancestors: Mapping[str, frozenset[str]]

    @classmethod
    def from_script_directory(cls) -> RevisionAncestry:
        script = _script_directory()
        mapping: dict[str, frozenset[str]] = {}
        for revision in script.walk_revisions(base="base", head="heads"):
            lineage = {revision.revision}
            parent = revision.down_revision
            while isinstance(parent, str):
                lineage.add(parent)
                parent_revision = script.get_revision(parent)
                if parent_revision is None:
                    break
                parent = parent_revision.down_revision
            mapping[revision.revision] = frozenset(lineage)
        return cls(MappingProxyType(mapping))

    def contains(self, current: str, required: str) -> bool:
        return required in self.ancestors.get(current, frozenset())


REVISION_ANCESTRY = RevisionAncestry.from_script_directory()


__all__ = ["REVISION_ANCESTRY", "RevisionAncestry"]
