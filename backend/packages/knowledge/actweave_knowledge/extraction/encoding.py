"""Bounded strict decoding, adapted from Dify extractor/helpers.py.

Upstream 9c16c865977e9d89a9ec7ae0536e893f4385a758; see UPSTREAM.md.
The detector is for the local parsing child's main thread, never an event loop.
"""

from __future__ import annotations

import asyncio
import signal
import threading
from pathlib import Path

from charset_normalizer import from_bytes

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from .contracts import ExtractionError, ExtractionLimits, ParseWarning

_SAMPLE_BYTES = 1024 * 1024


def read_source_bytes(path: Path) -> bytes:
    """Read incrementally and cap the actual bytes, including a growing source."""
    limit = ExtractionLimits().max_source_bytes
    data = bytearray()
    try:
        with path.open("rb") as source:
            while chunk := source.read(min(64 * 1024, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "原文件大小超过限制")
    except OSError:
        raise ExtractionError("TEXT_DECODING_FAILED") from None
    return bytes(data)


def detect_encoding(sample: bytes) -> str:
    """Dify's best-candidate detection, with an interruptible POSIX budget.

    Unlike upstream's from_path/ThreadPoolExecutor this reads only the supplied
    1 MiB sample and cannot leave an uninterruptible detector thread running.
    Runtime composition must call this in the parser child, not the host.
    """
    if threading.current_thread() is not threading.main_thread():
        raise ExtractionError("TEXT_DECODING_FAILED")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ExtractionError("TEXT_DECODING_FAILED")

    def expired(signum, frame):
        raise ExtractionError("TEXT_DECODING_FAILED", "编码探测超时")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 5)
        candidate = from_bytes(sample[:_SAMPLE_BYTES]).best()
        if candidate is None or not candidate.encoding:
            raise ExtractionError("TEXT_DECODING_FAILED")
        return candidate.encoding
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError("TEXT_DECODING_FAILED") from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def decode_text_file(path: Path) -> tuple[str, str, tuple[ParseWarning, ...]]:
    """BOM, strict UTF-8, then one sampled candidate; full decoding is strict."""
    data = read_source_bytes(path)
    boms = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")) or (data and any(bom.startswith(data) and bom != data for bom in boms)):
        raise ExtractionError("TEXT_DECODING_FAILED")
    encoding = "utf-8"
    if data.startswith(boms[0]):
        encoding = "utf-8-sig"
    elif data.startswith(boms[1:]):
        encoding = "utf-16"
    warnings: tuple[ParseWarning, ...] = ()
    try:
        text = data.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        if encoding != "utf-8":
            raise ExtractionError("TEXT_DECODING_FAILED") from None
        encoding = detect_encoding(data[:_SAMPLE_BYTES])
        try:
            text = data.decode(encoding, errors="strict")
        except (UnicodeError, LookupError):
            raise ExtractionError("TEXT_DECODING_FAILED") from None
        warnings = (ParseWarning(code="ENCODING_DETECTED", message="已使用探测编码严格解码", source_position={"encoding": encoding}),)
    # This precedes line provenance construction; no later global strip is used.
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding, warnings


def source_lines(text: str) -> list[str]:
    """Split normalized physical LF lines, retaining their terminators."""
    pieces = text.split("\n")
    return [part + "\n" for part in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])
