"""Routing and admission boundaries for the isolated extraction package."""

from __future__ import annotations

import importlib
import stat
import zipfile
from dataclasses import replace

import pytest
from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError
from actweave_knowledge.extraction.base import BaseExtractor
from actweave_knowledge.extraction.contracts import ExtractionError, ExtractionLimits
from parsing_test_helpers import make_context, make_document, make_setting


@pytest.fixture
def registry_module():
    return importlib.import_module("actweave_knowledge.extraction.registry")


@pytest.fixture
def signatures():
    return importlib.import_module("actweave_knowledge.extraction.signatures")


@pytest.mark.parametrize("etl,parser", [("builtin", "builtin.markdown"), ("unstructured_local", "unstructured.markdown")])
def test_markdown_modes(registry_module, etl, parser):
    assert registry_module.default_registry().resolve(datasource_type="file", etl_type=etl, extension=".MARKDOWN").extractor_id == parser


@pytest.mark.parametrize("ext,parser", [(".eml", "unstructured.eml"), (".msg", "unstructured.msg"), (".xml", "unstructured.xml")])
def test_long_tail_local_only(registry_module, ext, parser):
    registry = registry_module.default_registry()
    assert registry.resolve(datasource_type="file", etl_type="unstructured_local", extension=ext).extractor_id == parser
    with pytest.raises(ExtractionError, match="文件解析失败") as caught:
        registry.resolve(datasource_type="file", etl_type="builtin", extension=ext)
    assert caught.value.reason_code == "UNSUPPORTED_FORMAT"


@pytest.mark.parametrize("ext,etl", [(".doc", "builtin"), (".exe", "unstructured_local"), (".zip", "builtin"), ("txt", "builtin")])
def test_disallowed_formats_never_fall_back(registry_module, ext, etl):
    with pytest.raises(ExtractionError) as caught:
        registry_module.default_registry().resolve(datasource_type="file", etl_type=etl, extension=ext)
    assert caught.value.reason_code == "UNSUPPORTED_FORMAT"


@pytest.mark.parametrize("datasource,etl", [("web", "builtin"), ("file", "unknown")])
def test_unknown_routing_dimensions_fail(registry_module, datasource, etl):
    with pytest.raises(ExtractionError) as caught:
        registry_module.default_registry().resolve(datasource_type=datasource, etl_type=etl, extension=".txt")
    assert caught.value.reason_code == "UNSUPPORTED_FORMAT"


def test_duplicate_routes_rejected_case_insensitively(registry_module):
    item = registry_module.default_registry().resolve(datasource_type="file", etl_type="builtin", extension=".txt")
    with pytest.raises(ValueError, match="duplicate"):
        registry_module.ExtractorRegistry((item, replace(item, extensions=(".TXT",))))


def test_xls_does_not_inherit_xlsx_image_capability(registry_module):
    registry = registry_module.default_registry()
    assert registry.resolve(datasource_type="file", etl_type="builtin", extension=".xlsx").supports_embedded_images is True
    assert registry.resolve(datasource_type="file", etl_type="builtin", extension=".xls").supports_embedded_images is False


OFFICE = [
    (".docx", "word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"),
    (".xlsx", "xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
    (".pptx", "ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"),
]


def write_office(path, part, mime, *, content_types=None, extra=()):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types if content_types is not None else f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/{part}" ContentType="{mime}"/></Types>')
        archive.writestr(part, "<root/>")
        for name, data in extra:
            archive.writestr(name, data)


@pytest.mark.parametrize(
    "part,mime,xml",
    [
        ("xl/workbook.xml", OFFICE[1][2], None),
        ("word/document.xml", OFFICE[1][2], None),
        ("word/document.xml", OFFICE[0][2], "<Types/>"),
        ("word/document.xml", OFFICE[0][2], "<broken"),
        ("word/document.xml", OFFICE[0][2], '<!DOCTYPE Types [<!ENTITY a "x">]><Types>&a;</Types>'),
    ],
)
def test_office_masquerade_or_invalid_declaration_rejected(signatures, tmp_path, part, mime, xml):
    path = tmp_path / "fake.docx"
    write_office(path, part, mime, content_types=xml)
    with pytest.raises(ExtractionError) as caught:
        signatures.validate_file_signature(path, ".docx", ExtractionLimits())
    assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"


@pytest.mark.parametrize("member", ["../escape", "/absolute", "word/../../escape", "..\\escape", "C:\\escape"])
def test_zip_unsafe_member_rejected_before_library_load(signatures, tmp_path, member):
    path = tmp_path / "unsafe.docx"
    write_office(path, OFFICE[0][1], OFFICE[0][2], extra=((member, "x"),))
    with pytest.raises(ExtractionError) as caught:
        signatures.validate_file_signature(path, ".docx", ExtractionLimits())
    assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"
    assert not (tmp_path / "escape").exists()


def test_zip_symlink_member_rejected(signatures, tmp_path):
    path = tmp_path / "unsafe.docx"
    link = zipfile.ZipInfo("word/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    write_office(path, OFFICE[0][1], OFFICE[0][2], extra=((link, "../../secret"),))
    with pytest.raises(ExtractionError) as caught:
        signatures.validate_file_signature(path, ".docx", ExtractionLimits())
    assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"


def test_zip_cumulative_expansion_budget(signatures, tmp_path):
    path = tmp_path / "large.docx"
    write_office(path, OFFICE[0][1], OFFICE[0][2], extra=(("a", "x" * 600), ("b", "x" * 600)))
    with pytest.raises(KnowledgeError) as caught:
        signatures.validate_file_signature(path, ".docx", ExtractionLimits(max_work_dir_bytes=1000))
    assert caught.value.code == KNOWLEDGE_QUOTA_EXCEEDED


@pytest.mark.parametrize("mimetype,container,accepted", [("application/epub+zip", True, True), ("application/zip", True, False), ("application/epub+zip", False, False)])
def test_epub_identity(signatures, tmp_path, mimetype, container, accepted):
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", mimetype)
        if container:
            archive.writestr("META-INF/container.xml", "<container/>")
    if accepted:
        signatures.validate_file_signature(path, ".epub", ExtractionLimits())
    else:
        with pytest.raises(ExtractionError) as caught:
            signatures.validate_file_signature(path, ".epub", ExtractionLimits())
        assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"


@pytest.mark.parametrize(
    "ext,payload,accepted",
    [
        (".pdf", b"plain", False),
        (".docx", b"PK\x03\x04broken", False),
        (".msg", b"plain", False),
        (".md", b"%PDF-1.7", False),
        (".csv", b"PK\x03\x04", False),
        (".xml", bytes.fromhex("d0cf11e0a1b11ae1"), False),
        (".txt", "文档".encode("utf-16"), True),
        (".txt", b"x" * (1024 * 1024) + b"\x00", False),
    ],
    ids=["fake-pdf", "broken-office", "fake-msg", "pdf-as-markdown", "zip-as-csv", "binary-as-xml", "utf16-text", "binary-tail"],
)
def test_binary_and_text_signature_boundaries(signatures, tmp_path, ext, payload, accepted):
    path = tmp_path / ("source" + ext)
    path.write_bytes(payload)
    if accepted:
        signatures.validate_file_signature(path, ext, ExtractionLimits())
    else:
        with pytest.raises(ExtractionError) as caught:
            signatures.validate_file_signature(path, ext, ExtractionLimits())
        assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"


@pytest.fixture
def processor_case(registry_module, tmp_path):
    path = tmp_path / "staged-without-extension"
    path.write_text("hello")
    context = make_context(tmp_path / "work")
    calls = []
    documents = [make_document("abc"), make_document("def")]

    class Recorder(BaseExtractor):
        def extract(self, setting, context):
            calls.append("extract")
            return documents

    def factory():
        calls.append("factory")
        return Recorder()

    def probe():
        calls.append("probe")
        return None

    item = registry_module.default_registry().resolve(datasource_type="file", etl_type="builtin", extension=".txt")
    registration = replace(item, factory=factory, dependency_probe=probe)
    setting = make_setting(tmp_path / "source.txt", source_path=path, original_name="original.TXT")
    return setting, context, registration, calls, documents


def run_processor(registry_module, setting, context, registration):
    processor = importlib.import_module("actweave_knowledge.extraction.processor")
    return processor.ExtractProcessor(registry_module.ExtractorRegistry((registration,))).extract(setting, context)


def test_processor_uses_original_name_and_returns_documents(registry_module, processor_case):
    setting, context, registration, calls, documents = processor_case
    assert run_processor(registry_module, setting, context, registration) == documents
    assert calls == ["probe", "factory", "extract"]


@pytest.mark.parametrize("field,value", [("extractor_id", "other"), ("extractor_version", "old-version")])
def test_profile_mismatch_precedes_dependency_probe(registry_module, processor_case, field, value):
    setting, context, registration, calls, _ = processor_case
    setting = setting.model_copy(update={"profile": setting.profile.model_copy(update={field: value})})
    with pytest.raises(ExtractionError) as caught:
        run_processor(registry_module, setting, context, registration)
    assert caught.value.reason_code == "PARSER_PROFILE_UNAVAILABLE"
    assert calls == []


def test_missing_dependency_never_instantiates_or_opens_source(registry_module, processor_case):
    setting, context, registration, calls, _ = processor_case
    setting.source_path.unlink()
    registration = replace(registration, dependency_probe=lambda: "PARSER_DEPENDENCY_UNAVAILABLE")
    with pytest.raises(ExtractionError) as caught:
        run_processor(registry_module, setting, context, registration)
    assert caught.value.reason_code == "PARSER_DEPENDENCY_UNAVAILABLE"
    assert calls == []


def test_source_limit_precedes_factory(registry_module, processor_case):
    setting, context, registration, calls, _ = processor_case
    context = context.model_copy(update={"limits": ExtractionLimits(max_source_bytes=4)})
    with pytest.raises(KnowledgeError) as caught:
        run_processor(registry_module, setting, context, registration)
    assert caught.value.code == KNOWLEDGE_QUOTA_EXCEEDED
    assert calls == ["probe"]


def test_signature_precedes_factory(registry_module, processor_case):
    setting, context, registration, calls, _ = processor_case
    setting.source_path.write_bytes(b"%PDF-1.7")
    with pytest.raises(ExtractionError) as caught:
        run_processor(registry_module, setting, context, registration)
    assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"
    assert calls == ["probe"]


def test_cumulative_text_budget_counts_all_documents(registry_module, processor_case):
    setting, context, registration, _, _ = processor_case
    context = context.model_copy(update={"limits": ExtractionLimits(max_text_chars=5)})
    with pytest.raises(KnowledgeError) as caught:
        run_processor(registry_module, setting, context, registration)
    assert caught.value.code == KNOWLEDGE_QUOTA_EXCEEDED


def test_corrupt_compressed_declaration_returns_safe_error(signatures, tmp_path):
    import struct

    path = tmp_path / "corrupt.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types>" + "x" * 100 + "</Types>")
        archive.writestr("word/document.xml", "<document/>")
    data = bytearray(path.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", data, 26)
    data[30 + name_length + extra_length] = 0xFF  # Invalid DEFLATE block type.
    path.write_bytes(data)
    with pytest.raises(ExtractionError) as caught:
        signatures.validate_file_signature(path, ".docx", ExtractionLimits())
    assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"
