"""Public-safe execution failure taxonomy."""

from __future__ import annotations

import re

from app.private_work.run_repository import PrivateRunUsageSnapshot

_PUBLIC_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


def is_public_error_code(value: str) -> bool:
    return _PUBLIC_ERROR_CODE.fullmatch(value) is not None


def _validate_attempt_usage(
    attempt_usage: PrivateRunUsageSnapshot | None,
) -> None:
    if attempt_usage is not None and type(attempt_usage) is not PrivateRunUsageSnapshot:
        raise TypeError("attempt_usage must be a PrivateRunUsageSnapshot or None")


class TransientExecutionError(RuntimeError):
    """A public-safe failure before an ambiguous external side effect."""

    def __init__(
        self,
        public_error_code: str,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> None:
        if not is_public_error_code(public_error_code):
            raise ValueError("transient execution error requires a public code")
        _validate_attempt_usage(attempt_usage)
        self.public_error_code = public_error_code
        self.attempt_usage = attempt_usage
        super().__init__(public_error_code)


class PermanentExecutionError(RuntimeError):
    """A deterministic public-safe failure that must not be retried."""

    def __init__(
        self,
        public_error_code: str,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> None:
        if not is_public_error_code(public_error_code):
            raise ValueError("permanent execution error requires a public code")
        _validate_attempt_usage(attempt_usage)
        self.public_error_code = public_error_code
        self.attempt_usage = attempt_usage
        super().__init__(public_error_code)


class AmbiguousExternalSideEffect(RuntimeError):
    """Execution may have crossed an external side-effect boundary."""

    def __init__(
        self,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> None:
        _validate_attempt_usage(attempt_usage)
        self.attempt_usage = attempt_usage
        super().__init__("external side-effect state is unknown")


__all__ = [
    "AmbiguousExternalSideEffect",
    "PermanentExecutionError",
    "TransientExecutionError",
    "is_public_error_code",
]
