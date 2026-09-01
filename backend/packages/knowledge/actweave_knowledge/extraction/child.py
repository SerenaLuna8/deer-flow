"""Fixed synchronous child entry; no host clients, credentials or arbitrary imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from .contracts import ExtractionContext, ExtractionError, ExtractionLimits, ExtractionResult, ExtractSetting
from .images import LocalAttachmentSink
from .manifest import canonical_parse_fingerprint, encode_manifest, validate_result
from .normalizer import normalize_documents
from .processor import ExtractProcessor

FRAME_BYTES = 64 * 1024
# Only these safe codes cross the process boundary; never native exception text.
ERROR_CODES = frozenset(
    {
        "PARSER_DEPENDENCY_UNAVAILABLE",
        "PARSER_PROFILE_UNAVAILABLE",
        "PARSER_RESOURCE_UNAVAILABLE",
        "PARSER_RESOURCE_DIGEST_MISMATCH",
        "PARSER_WORK_DIR_LIMIT_EXCEEDED",
        "PARSER_IMAGE_LIMIT_EXCEEDED",
        "PARSER_ENCODING_TIMEOUT",
        "ENCODING_DETECTION_FAILED",
        "ENCODING_DETECTION_TIMEOUT",
        "ENCODING_DECODE_FAILED",
        "FORMAT_SIGNATURE_MISMATCH",
        "UNSUPPORTED_FORMAT",
        "PARSER_FAILED",
        "PARSER_QUOTA_EXCEEDED",
        "TEXT_DECODING_FAILED",
        "TABULAR_PARSE_FAILED",
        "HEADER_RULE_INVALID",
    }
)


def _send(stream, frame: dict) -> None:
    payload = json.dumps(frame, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(payload) > FRAME_BYTES:
        raise ExtractionError("PARSER_OUTPUT_INVALID")
    stream.write(payload)
    stream.flush()


class StreamingSink(LocalAttachmentSink):
    def __init__(self, work_dir, limits, stream):
        super().__init__(work_dir=work_dir, limits=limits)
        self.stream = stream
        self.sent: set[str] = set()

    def accept(self, source_path, *, alt_text, source):
        attachment = super().accept(source_path, alt_text=alt_text, source=source)
        if attachment.ref not in self.sent:
            asset = next(asset for asset in self.assets if asset.attachment.ref == attachment.ref)
            payload = asset.model_dump(mode="json")
            payload["relative_path"] = "child/" + asset.relative_path
            _send(self.stream, {"type": "asset", "asset": payload})
            if sys.stdin.buffer.readline(FRAME_BYTES + 1) != b'{"type":"ack"}\n':
                raise ExtractionError("PARSER_OUTPUT_INVALID")
            self.sent.add(attachment.ref)
        return attachment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-fd", type=int, required=True)
    args = parser.parse_args()
    with os.fdopen(args.output_fd, "wb", buffering=0) as stream:
        try:
            data = sys.stdin.buffer.readline(FRAME_BYTES + 1)
            if len(data) > FRAME_BYTES or not data.endswith(b"\n"):
                raise ExtractionError("PARSER_OUTPUT_INVALID")
            request = json.loads(data)
            if set(request) != {"setting", "limits"}:
                raise ExtractionError("PARSER_OUTPUT_INVALID")
            setting = ExtractSetting.model_validate(request["setting"])
            limits = ExtractionLimits.model_validate(request["limits"])
            work_dir = Path.cwd()
            sink = StreamingSink(work_dir, limits, stream)
            context = ExtractionContext(work_dir=work_dir, sink=sink, limits=limits, check_cancelled=lambda: None)
            # Must stay on this process's main thread: encoding uses SIGALRM.
            documents = normalize_documents(ExtractProcessor().extract(setting, context))
            with setting.source_path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            result = ExtractionResult(
                documents=tuple(documents), attachments=tuple(asset.attachment for asset in sink.assets), warnings=tuple(sink.warnings), source_sha256=digest, parse_fingerprint=canonical_parse_fingerprint(setting.profile)
            )
            validate_result(result, limits)
            payload = encode_manifest(result)
            if len(payload) > limits.max_manifest_bytes:
                raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")
            (work_dir / "manifest.json").write_bytes(payload)
            _send(stream, {"type": "result", "relative_path": "child/manifest.json"})
        except Exception as error:
            code = error.reason_code if isinstance(error, ExtractionError) else "PARSER_FAILED"
            if isinstance(error, KnowledgeError) and error.code == KNOWLEDGE_QUOTA_EXCEEDED:
                code = "PARSER_QUOTA_EXCEEDED"
            _send(stream, {"type": "error", "reason_code": code if code in ERROR_CODES else "PARSER_FAILED"})


if __name__ == "__main__":
    main()
