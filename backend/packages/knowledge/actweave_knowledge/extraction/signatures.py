"""Conservative container preflight; this does not execute or unpack documents."""

from __future__ import annotations

import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from .contracts import ExtractionError, ExtractionLimits

_OLE = bytes.fromhex("d0cf11e0a1b11ae1")
_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OFFICE = {
    ".docx": ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"),
    ".xlsx": ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
    ".pptx": ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"),
}
_TEXT = frozenset({".txt", ".md", ".markdown", ".mdx", ".csv", ".html", ".htm", ".xml", ".eml"})
_CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"


def _mismatch() -> ExtractionError:
    return ExtractionError("FORMAT_SIGNATURE_MISMATCH")


def _validate_zip(archive: zipfile.ZipFile, extension: str, limits: ExtractionLimits) -> None:
    total = 0
    names: set[str] = set()
    for member in archive.infolist():
        normalized = member.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or PureWindowsPath(normalized).drive or stat.S_ISLNK(member.external_attr >> 16):
            raise _mismatch()
        # Ambiguous entries can be interpreted differently by downstream libraries.
        if normalized in names or member.flag_bits & 1:
            raise _mismatch()
        names.add(normalized)
        total += member.file_size
        if total > limits.max_work_dir_bytes:
            raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "文件解压大小超过限制")

    if extension == ".epub":
        if "mimetype" not in names or "META-INF/container.xml" not in names:
            raise _mismatch()
        # Read only the exact declaration length, never a huge mimetype member.
        with archive.open("mimetype") as source:
            if source.read(21) != b"application/epub+zip":
                raise _mismatch()
        return

    part, expected_mime = _OFFICE[extension]
    if part not in names or "[Content_Types].xml" not in names:
        raise _mismatch()
    # Parse declarations incrementally and discard finished XML elements;
    # reject DTD/entity declarations before ElementTree can expand them.
    with archive.open("[Content_Types].xml") as source:
        parser = ElementTree.XMLPullParser(events=("start", "end"))
        tail = b""
        root = None
        matched = False
        while chunk := source.read(64 * 1024):
            probe = (tail + chunk).replace(b"\x00", b"").upper()
            if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                raise _mismatch()
            tail = chunk[-32:]
            parser.feed(chunk)
            for event, element in parser.read_events():
                if root is None:
                    if element.tag != f"{{{_CONTENT_TYPES_NAMESPACE}}}Types":
                        raise _mismatch()
                    root = element
                if event == "end":
                    if element.tag == f"{{{_CONTENT_TYPES_NAMESPACE}}}Override" and element.get("PartName") == f"/{part}":
                        if element.get("ContentType") != expected_mime or matched:
                            raise _mismatch()
                        matched = True
                    element.clear()
                    root.clear()
        parser.close()
        if not matched:
            raise _mismatch()


def validate_file_signature(path: Path, extension: str, limits: ExtractionLimits) -> None:
    """Reject mismatched binary/container identity before a format library runs.

    OLE streams remain the XLS/MSG parser's responsibility. The ZIP declared
    expansion check is not a process peak-memory or work-directory guarantee.
    """
    extension = extension.lower()
    try:
        with path.open("rb") as source:
            prefix = source.read(8)
            if extension in _OFFICE or extension == ".epub":
                if not prefix.startswith(_ZIP):
                    raise _mismatch()
                with zipfile.ZipFile(path) as archive:
                    _validate_zip(archive, extension, limits)
            elif extension == ".pdf":
                if not prefix.startswith(b"%PDF-"):
                    raise _mismatch()
            elif extension in {".xls", ".msg"}:
                if prefix != _OLE:
                    raise _mismatch()
            elif extension in _TEXT:
                if prefix.startswith((*_ZIP, _OLE, b"%PDF-")):
                    raise _mismatch()
                if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
                    return  # Full strict UTF-16 decoding belongs to encoding.py.
                if b"\x00" in prefix:
                    raise _mismatch()
                while chunk := source.read(64 * 1024):
                    if b"\x00" in chunk:
                        raise _mismatch()
            else:
                raise ExtractionError("UNSUPPORTED_FORMAT")
    except (OSError, EOFError, zipfile.BadZipFile, zlib.error, KeyError, ElementTree.ParseError, RuntimeError, NotImplementedError):
        raise _mismatch() from None
