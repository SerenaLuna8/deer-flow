"""Real process/protocol attacks, launched only by test sandbox overrides."""

import json
import os
import signal
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
signal.signal(signal.SIGTERM, signal.SIG_IGN)
mode = sys.argv[2] if len(sys.argv) > 2 else "hang"
if mode == "budget":
    Path("overflow").write_bytes(b"x" * 2**20)
elif mode == "descendant":
    pid = os.fork()
    if pid:
        Path(sys.argv[1] + ".descendant").write_text(str(pid))
        os._exit(0)
elif mode != "hang":
    output_fd = int(sys.argv[3])
    request = json.loads(sys.stdin.buffer.readline())
    if mode.startswith("bad:"):
        frames = {"unknown": b'{"type":"unknown"}\n', "huge": b"x" * (65536 + 1), "truncated": b'{"type":', "duplicate": b'{"type":"result","type":"error"}\n', "empty": b"", "secret": b'{"type":"error","reason_code":"SECRET_CONTENT"}\n'}
        os.write(output_fd, frames[mode.split(":", 1)[1]])
        os._exit(0)
    if mode.startswith("result:"):
        import hashlib

        from actweave_knowledge.extraction.child import _send
        from actweave_knowledge.extraction.contracts import ExtractionResult, ExtractSetting
        from actweave_knowledge.extraction.manifest import canonical_parse_fingerprint, encode_manifest

        setting = ExtractSetting.model_validate(request["setting"])
        result = ExtractionResult(source_sha256=hashlib.sha256(setting.source_path.read_bytes()).hexdigest(), parse_fingerprint=canonical_parse_fingerprint(setting.profile))
        if mode == "result:source":
            result = result.model_copy(update={"source_sha256": "0" * 64})
        if mode == "result:profile":
            result = result.model_copy(update={"parse_fingerprint": "0" * 64})
        Path("manifest.json").write_bytes(encode_manifest(result))
        output = os.fdopen(output_fd, "wb", buffering=0)
        _send(output, {"type": "result", "relative_path": "child/manifest.json"})
        if mode == "result:twice":
            _send(output, {"type": "result", "relative_path": "child/manifest.json"})
        os._exit(0)
    from actweave_knowledge.extraction.child import _send
    from actweave_knowledge.extraction.contracts import ExtractionLimits, SourceSpan
    from actweave_knowledge.extraction.images import LocalAttachmentSink
    from PIL import Image

    Image.new("RGB", (2, 2), "red").save("original.png")
    sink = LocalAttachmentSink(Path.cwd(), ExtractionLimits())
    sink.accept(Path("original.png"), alt_text="", source=SourceSpan(block_id="x", start=0, end=0, location={"page": 1}))
    asset = sink.assets[0].model_dump(mode="json")
    asset["relative_path"] = "child/" + asset["relative_path"]
    output = os.fdopen(output_fd, "wb", buffering=0)
    _send(output, {"type": "asset", "asset": asset})
    ack = sys.stdin.buffer.readline()
    Path("ack").write_bytes(ack)
    if mode == "asset_eof":
        os._exit(0)
    if mode == "asset_missing":
        import hashlib

        from actweave_knowledge.extraction.contracts import ExtractionResult, ExtractSetting
        from actweave_knowledge.extraction.manifest import canonical_parse_fingerprint, encode_manifest

        setting = ExtractSetting.model_validate(request["setting"])
        result = ExtractionResult(source_sha256=hashlib.sha256(setting.source_path.read_bytes()).hexdigest(), parse_fingerprint=canonical_parse_fingerprint(setting.profile))
        Path("manifest.json").write_bytes(encode_manifest(result))
        _send(output, {"type": "result", "relative_path": "child/manifest.json"})
        os._exit(0)
while True:
    signal.pause()
