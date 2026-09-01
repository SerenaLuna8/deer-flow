from __future__ import annotations

import hashlib
import os
import signal
import socket
from contextlib import contextmanager
from pathlib import Path

import pytest
from actweave_knowledge.extraction.contracts import Attachment, AttachmentOccurrence, ExtractionError, ExtractionLimits, LocalAttachment, SourceSpan
from actweave_knowledge.extraction.images import ImageRejected, LocalAttachmentSink, normalize_image
from actweave_knowledge.extraction.ipc import receive_asset
from PIL import Image, PngImagePlugin


def _span(page: int = 1) -> SourceSpan:
    return SourceSpan(block_id=f"page:{page}:image", start=0, end=0, location={"page": page})


def _png(path: Path, color="red", *, metadata: bool = False) -> Path:
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("author", "private author")
    with Image.new("RGB", (8, 6), color) as image:
        image.save(path, pnginfo=info)
    return path


def _asset(path: Path, *, relative_path: str | None = None) -> LocalAttachment:
    data = path.read_bytes()
    with Image.open(path) as image:
        width, height = image.size
    return LocalAttachment(attachment=Attachment(ref=hashlib.sha256(data).hexdigest(), media_type="image/png", size_bytes=len(data), width=width, height=height), relative_path=relative_path or "child/" + path.name)


@contextmanager
def _deadline():
    """Make a blocking FIFO regression fail, without leaking a worker thread."""

    def expired(signum, frame):
        raise AssertionError("nonregular file open blocked")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_normalization_deduplicates_pixels_strips_metadata_and_keeps_occurrences(tmp_path):
    source = _png(tmp_path / "source.png", metadata=True)
    other = _png(tmp_path / "other.png")
    sink = LocalAttachmentSink(tmp_path / "child", ExtractionLimits(max_images=1))
    first = sink.accept(source, alt_text="页1", source=_span(1))
    second = sink.accept(other, alt_text="页2", source=_span(2))
    occurrences = (AttachmentOccurrence(ref=first.ref, alt_text="页1", source=_span(1)), AttachmentOccurrence(ref=second.ref, alt_text="页2", source=_span(2)))
    assert first == second
    assert len(sink.assets) == 1
    assert [item.source.location["page"] for item in occurrences] == [1, 2]
    output = sink.work_dir / sink.assets[0].relative_path
    assert hashlib.sha256(output.read_bytes()).hexdigest() == first.ref
    with Image.open(output) as result:
        assert result.format == "PNG"
        assert result.info == {}
        assert result.convert("RGBA").getpixel((0, 0)) == (255, 0, 0, 255)


@pytest.mark.parametrize("mode,pixel,want", [("L", 123, (123, 123, 123, 255)), ("RGBA", (20, 40, 60, 70), (20, 40, 60, 70)), ("RGB", (20, 40, 60), (20, 40, 60, 255))])
def test_normalization_preserves_visible_pixels(tmp_path, mode, pixel, want):
    source = tmp_path / "source.png"
    with Image.new(mode, (3, 2), pixel) as image:
        image.save(source)
    result = normalize_image(source, tmp_path / "normalized", ExtractionLimits())
    with Image.open(tmp_path / "normalized" / result.relative_path) as image:
        assert image.convert("RGBA").getpixel((0, 0)) == want


def test_exif_orientation_is_applied_and_removed(tmp_path):
    source = tmp_path / "source.png"
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private author"
    with Image.new("RGB", (3, 2), "red") as image:
        image.putpixel((0, 0), (0, 0, 255))
        image.save(source, exif=exif)
    result = normalize_image(source, tmp_path / "normalized", ExtractionLimits())
    assert (result.attachment.width, result.attachment.height) == (2, 3)
    with Image.open(tmp_path / "normalized" / result.relative_path) as image:
        assert image.info == {}
        assert image.convert("RGB").getpixel((1, 0)) == (0, 0, 255)


@pytest.mark.parametrize("extension", ["gif", "tiff", "webp"])
def test_multiframe_uses_first_frame_and_warns_per_occurrence(tmp_path, extension):
    source = tmp_path / f"source.{extension}"
    with Image.new("RGB", (4, 3), "red") as first, Image.new("RGB", (4, 3), "blue") as second:
        first.save(source, save_all=True, append_images=[second], lossless=True)
    sink = LocalAttachmentSink(tmp_path / "child", ExtractionLimits())
    for page in (1, 2):
        sink.accept(source, alt_text="图", source=_span(page))
    assert len(sink.assets) == 1
    assert [(warning.code, warning.source_position) for warning in sink.warnings] == [("IMAGE_FIRST_FRAME_ONLY", {"page": 1}), ("IMAGE_FIRST_FRAME_ONLY", {"page": 2})]
    with Image.open(sink.work_dir / sink.assets[0].relative_path) as image:
        assert image.n_frames == 1
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_pixel_limit_rejects_before_decode(tmp_path, monkeypatch):
    source = _png(tmp_path / "source.png")

    def forbidden_load(self, *args, **kwargs):
        pytest.fail("pixel budget must be checked before full decode")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_load)
    sink = LocalAttachmentSink(tmp_path / "child", ExtractionLimits(max_image_pixels=47))
    with pytest.raises(ImageRejected) as caught:
        sink.accept(source, alt_text="图", source=_span(2))
    assert caught.value.warning.code == "IMAGE_LIMIT_EXCEEDED"
    assert caught.value.warning.source_position == {"page": 2}


def test_pillow_bomb_warning_is_a_controlled_limit_rejection(tmp_path, monkeypatch):
    source = _png(tmp_path / "source.png")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 30)
    with pytest.raises(ImageRejected) as caught:
        normalize_image(source, tmp_path / "out", ExtractionLimits())
    assert caught.value.warning.code == "IMAGE_LIMIT_EXCEEDED"


@pytest.mark.parametrize("data", [b"broken PNG", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'])
def test_invalid_and_script_svg_are_rejected_without_output(tmp_path, data):
    source = tmp_path / "source"
    source.write_bytes(data)
    with pytest.raises(ImageRejected) as caught:
        normalize_image(source, tmp_path / "out", ExtractionLimits())
    assert caught.value.warning.code == "IMAGE_CORRUPT"
    assert str(tmp_path) not in str(caught.value.warning)
    assert not list((tmp_path / "out").glob("*"))


@pytest.mark.parametrize("limit", ["max_image_bytes", "max_images", "max_total_image_bytes"])
def test_sink_budget_rejection_removes_only_new_output(tmp_path, limit):
    red = _png(tmp_path / "red.png")
    blue = _png(tmp_path / "blue.png", "blue")
    sample = normalize_image(red, tmp_path / "sample", ExtractionLimits()).attachment
    limits = ExtractionLimits(**{limit: sample.size_bytes if limit == "max_total_image_bytes" else 1})
    sink = LocalAttachmentSink(tmp_path / "child", limits)
    if limit != "max_image_bytes":
        original = sink.accept(red, alt_text="图", source=_span())
        assert sink.accept(red, alt_text="重复", source=_span(2)) == original
    before = list(sink.work_dir.glob("*"))
    with pytest.raises(ImageRejected) as caught:
        sink.accept(blue, alt_text="图", source=_span(3))
    assert caught.value.warning.code == "IMAGE_LIMIT_EXCEEDED"
    assert list(sink.work_dir.glob("*")) == before


def test_filesystem_errors_are_not_downgraded_to_image_warning(tmp_path):
    with pytest.raises(FileNotFoundError):
        normalize_image(tmp_path / "missing", tmp_path / "out", ExtractionLimits())
    source = _png(tmp_path / "source.png")
    output = tmp_path / "output"
    output.write_text("not a directory")
    with pytest.raises(OSError) as caught:
        normalize_image(source, output, ExtractionLimits())
    assert not isinstance(caught.value, ImageRejected)


def test_parent_copies_to_received_and_survives_child_replacement(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    path = _png(child / "image.png")
    asset = _asset(path)
    accepted = {}
    result = receive_asset(asset, work_dir=tmp_path, limits=ExtractionLimits(), accepted=accepted)
    path.unlink()
    path.write_bytes(b"replaced by child")
    assert result.relative_path == f"received/{asset.attachment.ref}.png"
    received = tmp_path / result.relative_path
    assert hashlib.sha256(received.read_bytes()).hexdigest() == asset.attachment.ref
    assert accepted == {asset.attachment.ref: asset.attachment}
    assert received.stat().st_nlink == 1
    assert (received.parent.stat().st_mode & 0o077) == 0


@pytest.mark.parametrize(
    "relative_path", ["/child/image.png", "../child/image.png", "child/../image.png", "child//image.png", "child/./image.png", "child/image.png/", "received/image.png", "child", "child\\image.png", "child/image\x00.png"]
)
def test_parent_rejects_unsafe_paths(tmp_path, relative_path):
    path = _png(tmp_path / "image.png")
    with pytest.raises(ExtractionError) as caught:
        receive_asset(_asset(path, relative_path=relative_path), work_dir=tmp_path, limits=ExtractionLimits(), accepted={})
    assert caught.value.reason_code == "PARSER_OUTPUT_INVALID"


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo", "directory", "socket"])
def test_parent_promptly_rejects_nonregular_and_linked_files(tmp_path, kind, monkeypatch):
    outside = _png(tmp_path / "outside.png")
    child = tmp_path / "child"
    child.mkdir()
    path = child / "image.png"
    sock = None
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "directory_symlink":
        child.rmdir()
        child.symlink_to(tmp_path, target_is_directory=True)
        path = child / outside.name
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        sock = socket.socket(socket.AF_UNIX)
        # macOS sockaddr_un cannot contain pytest's long absolute temp path.
        monkeypatch.chdir(child)
        sock.bind(path.name)
    try:
        with _deadline(), pytest.raises(ExtractionError) as caught:
            receive_asset(_asset(outside, relative_path="child/" + path.name), work_dir=tmp_path, limits=ExtractionLimits(), accepted={})
        assert caught.value.reason_code == "PARSER_OUTPUT_INVALID"
    finally:
        if sock is not None:
            sock.close()


@pytest.mark.parametrize("field,value", [("ref", "a" * 64), ("size_bytes", 1), ("width", 1), ("height", 1), ("media_type", "image/jpeg")])
def test_parent_rejects_forged_descriptors_without_registering_or_leaving_copy(tmp_path, field, value):
    child = tmp_path / "child"
    child.mkdir()
    asset = _asset(_png(child / "image.png"))
    asset = asset.model_copy(update={"attachment": asset.attachment.model_copy(update={field: value})})
    accepted = {}
    with pytest.raises(ExtractionError) as caught:
        receive_asset(asset, work_dir=tmp_path, limits=ExtractionLimits(), accepted=accepted)
    assert caught.value.reason_code == "PARSER_OUTPUT_INVALID"
    assert accepted == {}
    assert not list((tmp_path / "received").glob("*"))


@pytest.mark.parametrize("kind", ["jpeg", "metadata", "corrupt"])
def test_parent_rejects_noncanonical_or_corrupt_png(tmp_path, kind):
    child = tmp_path / "child"
    child.mkdir()
    path = _png(child / "image.png", metadata=kind == "metadata")
    if kind == "jpeg":
        with Image.new("RGB", (8, 6)) as image:
            image.save(path, format="JPEG")
    asset = _asset(path)
    if kind == "corrupt":
        data = path.read_bytes()[:-8]
        path.write_bytes(data)
        asset = asset.model_copy(update={"attachment": asset.attachment.model_copy(update={"ref": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})})
    with pytest.raises(ExtractionError):
        receive_asset(asset, work_dir=tmp_path, limits=ExtractionLimits(), accepted={})


@pytest.mark.parametrize("limit", ["max_images", "max_total_image_bytes", "max_image_bytes", "max_image_pixels", "max_work_dir_bytes"])
def test_parent_enforces_actual_budgets_and_deduplicates_repeated_refs(tmp_path, limit):
    child = tmp_path / "child"
    child.mkdir()
    first = _asset(_png(child / "red.png"))
    second = _asset(_png(child / "blue.png", "blue"))
    value = first.attachment.size_bytes if limit == "max_total_image_bytes" else 1
    limits = ExtractionLimits(**{limit: value})
    accepted = {}
    if limit in {"max_images", "max_total_image_bytes"}:
        result = receive_asset(first, work_dir=tmp_path, limits=limits, accepted=accepted)
        assert receive_asset(first, work_dir=tmp_path, limits=limits, accepted=accepted) == result
    before = dict(accepted)
    with pytest.raises(ExtractionError):
        receive_asset(second, work_dir=tmp_path, limits=limits, accepted=accepted)
    assert accepted == before
    assert len(list((tmp_path / "received").glob("*"))) == len(before)


def test_parent_revalidates_duplicate_bytes_not_just_ref(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    path = _png(child / "image.png")
    first = _asset(path)
    accepted = {}
    received = receive_asset(first, work_dir=tmp_path, limits=ExtractionLimits(), accepted=accepted)
    original = (tmp_path / received.relative_path).read_bytes()
    path.write_bytes(b"x" * first.attachment.size_bytes)
    with pytest.raises(ExtractionError):
        receive_asset(first, work_dir=tmp_path, limits=ExtractionLimits(), accepted=accepted)
    assert (tmp_path / received.relative_path).read_bytes() == original
    assert accepted == {first.attachment.ref: first.attachment}


def test_normalization_work_directory_budget_is_fatal(tmp_path):
    source = _png(tmp_path / "source.png")
    output = tmp_path / "output"
    output.mkdir()
    (output / "conversion.tmp").write_bytes(b"x" * 100)
    with pytest.raises(ExtractionError):
        normalize_image(source, output, ExtractionLimits(max_work_dir_bytes=99))
    assert [path.name for path in output.iterdir()] == ["conversion.tmp"]


def test_normalization_budget_counts_source_and_output_together(tmp_path):
    source = _png(tmp_path / "source.png")
    sample = normalize_image(source, tmp_path / "sample", ExtractionLimits())
    limit = max(source.stat().st_size, sample.attachment.size_bytes)
    with pytest.raises(ExtractionError):
        normalize_image(source, tmp_path / "out", ExtractionLimits(max_work_dir_bytes=limit))
    assert not list((tmp_path / "out").glob("*"))


@pytest.mark.parametrize(
    "edge,limits",
    [
        pytest.param(64, ExtractionLimits(max_source_bytes=1024), id="scaled-upload-limit"),
        pytest.param(4200, ExtractionLimits(), id="default-limits-large-extracted-bmp"),
    ],
)
def test_extracted_image_is_not_limited_by_original_document_upload_size(tmp_path, edge, limits):
    source = tmp_path / "extracted.bmp"
    with Image.new("RGB", (edge, edge), "red") as image:
        image.save(source)
    raw_size = source.stat().st_size
    assert limits.max_source_bytes < raw_size < limits.max_work_dir_bytes
    assert edge * edge <= limits.max_image_pixels

    result = normalize_image(source, tmp_path / "normalized", limits)

    assert (result.attachment.width, result.attachment.height) == (edge, edge)
    assert result.attachment.size_bytes <= limits.max_image_bytes
    assert raw_size + result.attachment.size_bytes <= limits.max_work_dir_bytes
    output = tmp_path / "normalized" / result.relative_path
    data = output.read_bytes()
    assert len(data) == result.attachment.size_bytes
    assert hashlib.sha256(data).hexdigest() == result.attachment.ref
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_parent_received_directory_cannot_be_a_symlink(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    asset = _asset(_png(child / "image.png"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "received").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExtractionError) as caught:
        receive_asset(asset, work_dir=tmp_path, limits=ExtractionLimits(), accepted={})
    assert caught.value.reason_code == "PARSER_OUTPUT_INVALID"
    assert list(outside.iterdir()) == []
