"""Base interface for local extraction adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .contracts import Document, ExtractionContext, ExtractSetting


class BaseExtractor(ABC):
    """Extract one admitted local source without using host persistence."""

    @abstractmethod
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        """Return normalized documents for the setting's local source file."""
