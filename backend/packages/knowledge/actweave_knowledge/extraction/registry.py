"""Explicit local parser routing and the capability source of truth.

Factories import adapters only when extraction starts. Versions fingerprint the
actual installed parsing dependencies and verified platform resources.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata

from .base import BaseExtractor
from .contracts import ExtractionError
from .runtime_resources import ADAPTER_REVISION, probe_parser_resources, runtime_digest

UPSTREAM_COMMIT = "9c16c865977e9d89a9ec7ae0536e893f4385a758"
_BOTH = ("dify", "unstructured_local")
_DEPENDENCY_PINS = {
    "beautifulsoup4": "4.14.3",
    "charset-normalizer": "3.4.7",
    "markdown-it-py": "4.2.0",
    "openpyxl": "3.1.5",
    "pandas": "3.0.2",
    "pillow": "12.3.0",
    "pypdfium2": "5.7.1",
    "python-docx": "1.2.0",
    "python-pptx": "1.0.2",
    "xlrd": "2.0.2",
    "unstructured": "0.21.5",
    "python-oxmsg": "0.0.2",
    "pypandoc-binary": "1.17",
    "python-magic": "0.4.27",
    # Unstructured 0.21.5 pins this model in nlp/tokenize.py; P1-T7 bundles it.
    "en-core-web-sm": "3.8.0",
}


@dataclass(frozen=True)
class ExtractorRegistration:
    extractor_id: str
    extractor_version: str
    extensions: tuple[str, ...]
    etl_types: tuple[str, ...]
    supports_embedded_images: bool
    factory: Callable[[], BaseExtractor]
    dependency_probe: Callable[[], str | None]


class ExtractorRegistry:
    def __init__(self, registrations: tuple[ExtractorRegistration, ...]):
        self.registrations = tuple(registrations)
        self._routes: dict[tuple[str, str], ExtractorRegistration] = {}
        for item in self.registrations:
            for etl in item.etl_types:
                for extension in item.extensions:
                    key = (etl, extension.lower())
                    if key in self._routes:
                        raise ValueError("duplicate extractor route")
                    self._routes[key] = item

    def resolve(self, *, datasource_type: str, etl_type: str, extension: str) -> ExtractorRegistration:
        if datasource_type != "file" or etl_type not in _BOTH:
            raise ExtractionError("UNSUPPORTED_FORMAT")
        item = self._routes.get((etl_type, extension.lower()))
        if item is None:
            raise ExtractionError("UNSUPPORTED_FORMAT")
        return item


def _installed_version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _registration(
    extractor_id: str,
    module: str,
    class_name: str,
    extensions: tuple[str, ...],
    etl_types: tuple[str, ...],
    dependencies: tuple[str, ...],
    digest: str,
    *,
    images: bool = False,
) -> ExtractorRegistration:
    def dependency_probe() -> str | None:
        # Version admission and actual resource bytes both gate availability.
        for package in dependencies:
            actual = _installed_version(package)
            if actual is None or (package in _DEPENDENCY_PINS and actual != _DEPENDENCY_PINS[package]):
                return "PARSER_DEPENDENCY_UNAVAILABLE"
        return probe_parser_resources(extractor_id)

    def factory() -> BaseExtractor:
        try:
            adapter = importlib.import_module(f".{module}", package=__package__)
            return getattr(adapter, class_name)()
        except (ImportError, AttributeError):
            # Adapters arrive in P1-T3/T4/T6/T7; no text fallback is permitted.
            raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE") from None

    return ExtractorRegistration(
        extractor_id=extractor_id,
        extractor_version=f"{'unstructured_local' if extractor_id.startswith('unstructured.') else 'dify'}:{UPSTREAM_COMMIT}:{ADAPTER_REVISION}:{digest}",
        extensions=extensions,
        etl_types=etl_types,
        supports_embedded_images=images,
        factory=factory,
        dependency_probe=dependency_probe,
    )


def default_registry() -> ExtractorRegistry:
    """Build 14 registration groups; legacy XLS has its own image capability."""
    digest = runtime_digest()
    local = ("unstructured", "python-magic", "lxml", "spacy", "en-core-web-sm")
    return ExtractorRegistry(
        (
            _registration("dify.text", "builtin.text_extractor", "TextExtractor", (".txt",), _BOTH, ("charset-normalizer",), digest),
            _registration("dify.markdown", "builtin.markdown_extractor", "MarkdownExtractor", (".md", ".markdown", ".mdx"), ("dify",), ("charset-normalizer", "markdown-it-py"), digest),
            _registration("dify.pdf", "builtin.pdf_extractor", "PdfExtractor", (".pdf",), _BOTH, ("pypdfium2", "pillow"), digest, images=True),
            _registration("dify.word", "builtin.word_extractor", "WordExtractor", (".docx",), _BOTH, ("python-docx", "lxml", "pillow"), digest, images=True),
            _registration("dify.excel", "builtin.excel_extractor", "ExcelExtractor", (".xlsx",), _BOTH, ("openpyxl", "pandas", "numpy", "pillow"), digest, images=True),
            _registration("dify.excel", "builtin.excel_extractor", "ExcelExtractor", (".xls",), _BOTH, ("pandas", "numpy", "xlrd"), digest),
            _registration("dify.csv", "builtin.csv_extractor", "CSVExtractor", (".csv",), _BOTH, ("charset-normalizer",), digest),
            _registration("dify.html", "builtin.html_extractor", "HtmlExtractor", (".html", ".htm"), _BOTH, ("beautifulsoup4", "charset-normalizer"), digest),
            _registration("unstructured.pptx", "unstructured_local.unstructured_pptx_extractor", "UnstructuredPPTXExtractor", (".pptx",), _BOTH, (*local, "python-pptx"), digest),
            _registration("unstructured.epub", "unstructured_local.unstructured_epub_extractor", "UnstructuredEpubExtractor", (".epub",), _BOTH, (*local, "pypandoc-binary"), digest),
            _registration(
                "unstructured.markdown", "unstructured_local.unstructured_markdown_extractor", "UnstructuredMarkdownExtractor", (".md", ".markdown", ".mdx"), ("unstructured_local",), (*local, "markdown", "charset-normalizer"), digest
            ),
            _registration("unstructured.eml", "unstructured_local.unstructured_eml_extractor", "UnstructuredEmlExtractor", (".eml",), ("unstructured_local",), local, digest),
            _registration("unstructured.msg", "unstructured_local.unstructured_msg_extractor", "UnstructuredMsgExtractor", (".msg",), ("unstructured_local",), (*local, "python-oxmsg", "olefile"), digest),
            _registration("unstructured.xml", "unstructured_local.unstructured_xml_extractor", "UnstructuredXmlExtractor", (".xml",), ("unstructured_local",), local, digest),
        )
    )
