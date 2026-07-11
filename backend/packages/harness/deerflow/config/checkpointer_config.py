"""Deprecated compatibility shim for PostgreSQL checkpointer configuration.

The independent ``checkpointer`` application setting has been removed. This
module remains temporarily so the task-3 runtime cleanup can delete its legacy
imports without coupling that cleanup to the public configuration migration.
"""

from __future__ import annotations

import warnings
from typing import Literal

from pydantic import BaseModel

from deerflow.config.database_config import DatabaseConfig


class CheckpointerConfig(BaseModel):
    """Deprecated PostgreSQL view derived exclusively from DatabaseConfig."""

    database: DatabaseConfig

    @property
    def type(self) -> Literal["postgres"]:
        return "postgres"

    @property
    def connection_string(self) -> str:
        return self.database.checkpointer_url


_checkpointer_config: CheckpointerConfig | None = None


def _warn_deprecated() -> None:
    warnings.warn(
        "CheckpointerConfig is deprecated; derive checkpointer settings from DatabaseConfig instead",
        DeprecationWarning,
        stacklevel=3,
    )


def get_checkpointer_config() -> CheckpointerConfig | None:
    """Return the temporary PostgreSQL compatibility view, if initialized."""
    return _checkpointer_config


def set_checkpointer_config(config: CheckpointerConfig | None) -> None:
    """Set the temporary compatibility view for legacy runtime imports."""
    if config is not None and not isinstance(config, CheckpointerConfig):
        raise TypeError("checkpointer compatibility state must be derived from DatabaseConfig")
    global _checkpointer_config
    _checkpointer_config = config


def ensure_config_loaded() -> None:
    """Load AppConfig and derive the temporary PostgreSQL compatibility view."""
    global _checkpointer_config
    if _checkpointer_config is not None:
        return

    from deerflow.config.app_config import _app_config, get_app_config

    if _app_config is not None:
        _checkpointer_config = CheckpointerConfig(database=_app_config.database)
        return
    try:
        config = get_app_config()
    except FileNotFoundError:
        return
    _checkpointer_config = CheckpointerConfig(database=config.database)


def load_checkpointer_config_from_dict(config_dict: dict | None) -> None:
    """Deprecated adapter accepting only the new DatabaseConfig dictionary."""
    global _checkpointer_config
    if config_dict is None:
        _checkpointer_config = None
        return
    _warn_deprecated()
    _checkpointer_config = CheckpointerConfig(database=DatabaseConfig.model_validate(config_dict))
