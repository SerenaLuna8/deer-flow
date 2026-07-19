"""Shared errors for project-scoped Thread metadata persistence."""


class InvalidMetadataFilterError(ValueError):
    """Raised when all client-supplied metadata filter keys are rejected."""
