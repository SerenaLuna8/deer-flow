"""P3-T4 gates for stateless P1 extraction and bounded preview projection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
from pathlib import Path

import pytest
from actweave_knowledge import KnowledgeChunkPreviewRequest, KnowledgeError, KnowledgeSettings
from actweave_knowledge.extraction.contracts import (
    Attachment,
    AttachmentOccurrence,
    Document,
    ExtractionError,
    ExtractionLimits,
    ExtractionResult,
    LocalAttachment,
    ParseWarning,
    SourceSpan,
)
from actweave_knowledge.extraction.manifest import canonical_parse_fingerprint
from actweave_knowledge.extraction.registry import default_registry
from actweave_knowledge.extraction.runtime import ParserSlots
from actweave_knowledge.ingestion.profiles import ProcessingParameters, build_file_capabilities
from parsing_test_helpers import make_parse_profile, write_docx_with_image
from PIL import Image


def _asset(work_dir: Path, *, color: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> LocalAttachment:
    path = work_dir / f"asset-{len(list(work_dir.iterdir()))}.png"
    Image.new("RGB", size, color).save(path, format="PNG")
    payload = path.read_bytes()
    return LocalAttachment(
        attachment=Attachment(
            ref=hashlib.sha256(payload).hexdigest(),
            media_type="image/png",
            size_bytes=len(payload),
            width=size[0],
            height=size[1],
        ),
        relative_path=path.name,
    )


def _request(path: Path, **overrides: object) -> KnowledgeChunkPreviewRequest:
    fields: dict[str, object] = {
        "original_name": path.name,
        "source_path": path,
        "size_bytes": path.stat().st_size,
        "processing_profile": ProcessingParameters(size=200, overlap=20, child_size=100),
    }
    fields.update(overrides)
    return KnowledgeChunkPreviewRequest(**fields)


async def _guard() -> None:
    return None


def test_general_token_profile_ignores_unused_default_child_size() -> None:
    from actweave_knowledge.ingestion.splitter import split_documents
    from parsing_test_helpers import make_chunk_profile, make_document

    drafts = split_documents(
        (make_document("有效正文"),),
        profile=make_chunk_profile(
            mode="general",
            size=200,
            overlap=0,
            child_size=500,
        ),
    )

    assert [draft.content for draft in drafts] == ["有效正文"]


def _snapshot_rows(tables: dict[str, list]) -> dict[str, list]:
    return {
        name: sorted(
            [tuple((column.name, repr(getattr(row, column.name))) for column in row.__table__.columns) for row in rows],
            key=repr,
        )
        for name, rows in tables.items()
    }


@pytest.mark.asyncio
async def test_preview_does_not_persist_rows_objects_tasks_or_call_models(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    from actweave_knowledge.extraction.contracts import ProcessingProfile
    from ingestion_test_helpers import ingestion_harness
    from parsing_test_helpers import make_chunk_profile

    async with ingestion_harness(postgres_database_url) as harness:
        source = tmp_path / "manual.docx"
        write_docx_with_image(source)
        profile = ProcessingProfile(
            parse=make_parse_profile(".docx"),
            chunk=make_chunk_profile(),
        )
        before = _snapshot_rows(await harness.resources.read_rows())
        objects_before = dict(harness.resources.object_store.objects)

        preview = await harness.preview(source, profile)

        assert preview.preview_attachments
        assert _snapshot_rows(await harness.resources.read_rows()) == before
        assert harness.resources.object_store.objects == objects_before
        assert harness.fake_model.calls == []


@pytest.mark.asyncio
async def test_module_shares_one_nonqueueing_preview_slot(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from actweave_knowledge.extraction.contracts import ProcessingProfile
    from actweave_knowledge.ingestion import preview as preview_module
    from ingestion_test_helpers import ingestion_harness
    from parsing_test_helpers import make_chunk_profile, make_document

    source = tmp_path / "busy.txt"
    source.write_text("并发预览正文", encoding="utf-8")
    profile = ProcessingProfile(
        parse=make_parse_profile(".txt"),
        chunk=make_chunk_profile(),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def run_extraction(setting, *, work_dir, limits, timeout_seconds, on_asset, guard):  # noqa: ANN001
        del setting, work_dir, limits, timeout_seconds, on_asset
        started.set()
        await release.wait()
        await guard()
        return ExtractionResult(
            documents=(make_document("并发预览正文"),),
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            parse_fingerprint=canonical_parse_fingerprint(profile.parse),
        )

    monkeypatch.setattr(preview_module, "run_extraction", run_extraction)
    async with ingestion_harness(postgres_database_url) as harness:
        first = asyncio.create_task(harness.preview(source, profile))
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(ExtractionError) as caught:
            await harness.preview(source, profile)
        assert caught.value.reason_code == "PARSER_BUSY"
        release.set()
        assert (await first).chunks[0].content == "并发预览正文"


def test_preview_assets_keep_logical_refs_and_emit_only_safe_bounded_bytes(tmp_path: Path) -> None:
    from actweave_knowledge.ingestion.preview_assets import make_preview_assets

    first = _asset(tmp_path, color=(255, 0, 0))
    second = _asset(tmp_path, color=(0, 0, 255))

    projected, omitted = make_preview_assets(
        (first, second),
        work_dir=tmp_path,
        selected_refs=(second.attachment.ref, first.attachment.ref, second.attachment.ref),
    )

    assert omitted == 0
    assert [item["ref"] for item in projected] == [second.attachment.ref, first.attachment.ref]
    assert all(item["media_type"] in {"image/png", "image/jpeg", "image/webp"} for item in projected)
    assert all(len(base64.b64decode(item["data_base64"], validate=True)) <= 128 * 1024 for item in projected)
    assert all(set(item) == {"ref", "media_type", "data_base64"} for item in projected)


def test_preview_assets_limit_distinct_refs_and_count_missing_or_corrupt_images(tmp_path: Path) -> None:
    from actweave_knowledge.ingestion.preview_assets import make_preview_assets

    assets = [_asset(tmp_path, color=(index, index, index)) for index in range(22)]
    corrupt_path = tmp_path / assets[0].relative_path
    corrupt_path.write_bytes(b"not-an-image")
    missing_path = tmp_path / assets[1].relative_path
    missing_path.unlink()

    projected, omitted = make_preview_assets(
        tuple(assets),
        work_dir=tmp_path,
        selected_refs=tuple(item.attachment.ref for item in assets),
    )

    assert len(projected) == 18
    assert omitted == 4
    assert {item["ref"] for item in projected}.isdisjoint({assets[0].attachment.ref, assets[1].attachment.ref})


def test_preview_assets_downsample_large_raster_and_enforce_combined_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from actweave_knowledge.ingestion import preview_assets

    random_bytes = random.Random(7).randbytes(512 * 512 * 3)
    noisy_path = tmp_path / "noise.png"
    Image.frombytes("RGB", (512, 512), random_bytes).save(noisy_path, format="PNG")
    payload = noisy_path.read_bytes()
    noisy = LocalAttachment(
        attachment=Attachment(
            ref=hashlib.sha256(payload).hexdigest(),
            media_type="image/png",
            size_bytes=len(payload),
            width=512,
            height=512,
        ),
        relative_path=noisy_path.name,
    )
    projected, omitted = preview_assets.make_preview_assets(
        (noisy,),
        work_dir=tmp_path,
        selected_refs=(noisy.attachment.ref,),
    )
    thumbnail = base64.b64decode(projected[0]["data_base64"], validate=True)
    assert len(payload) > 128 * 1024
    assert len(thumbnail) <= 128 * 1024
    assert omitted == 0

    first = _asset(tmp_path, color=(12, 34, 56))
    second = _asset(tmp_path, color=(65, 43, 21))
    baseline, _ = preview_assets.make_preview_assets(
        (first, second),
        work_dir=tmp_path,
        selected_refs=(first.attachment.ref, second.attachment.ref),
    )
    combined_size = sum(len(base64.b64decode(item["data_base64"], validate=True)) for item in baseline)
    monkeypatch.setattr(preview_assets, "MAX_PREVIEW_ATTACHMENTS_BYTES", combined_size - 1)
    projected, omitted = preview_assets.make_preview_assets(
        (first, second),
        work_dir=tmp_path,
        selected_refs=(first.attachment.ref, second.attachment.ref),
    )
    assert len(projected) == 1
    assert omitted == 1


@pytest.mark.asyncio
async def test_actual_docx_preview_uses_p1_runner_and_p3_splitter_without_exposing_paths(tmp_path: Path) -> None:
    from actweave_knowledge.ingestion.preview import preview_document_chunks

    source = tmp_path / "manual.docx"
    write_docx_with_image(source)
    settings = KnowledgeSettings()
    capabilities = build_file_capabilities(settings, default_registry())

    preview = await preview_document_chunks(
        _request(source),
        settings,
        capability_revision=capabilities.capability_revision,
        parser_slots=ParserSlots(1),
        guard=_guard,
        registry=default_registry(),
    )

    assert preview.total >= 1
    assert "设备手册" in preview.chunks[0].content
    assert preview.chunks[0].token_count > 0
    assert preview.chunks[0].source_spans
    assert preview.chunks[0].attachments
    assert preview.source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert preview.effective_profile.parse.extractor_id == "dify.word"
    assert preview.preview_fingerprint != preview.source_sha256
    assert preview.preview_attachments[0].ref == preview.chunks[0].attachments[0].ref
    assert preview.omitted_preview_attachment_count == 0
    assert preview.table_sources == ()
    assert "relative_path" not in repr(preview)


@pytest.mark.asyncio
async def test_table_sources_preserve_real_sheet_header_row_and_raw_cells(tmp_path: Path) -> None:
    from actweave_knowledge.ingestion.preview import preview_document_chunks

    source = tmp_path / "inventory.csv"
    source.write_text("说明行\n设备,端口\nR1,Gi0/0\n", encoding="utf-8")
    settings = KnowledgeSettings()
    capabilities = build_file_capabilities(settings, default_registry())
    parameters = ProcessingParameters(
        size=200,
        overlap=0,
        child_size=100,
        header_rules=({"sheet": None, "mode": "explicit", "row": 2},),
    )

    preview = await preview_document_chunks(
        _request(source, processing_profile=parameters),
        settings,
        capability_revision=capabilities.capability_revision,
        parser_slots=ParserSlots(1),
        guard=_guard,
        registry=default_registry(),
    )

    assert len(preview.table_sources) == 1
    table = preview.table_sources[0]
    assert (table.sheet, table.header_mode, table.header_row, table.header_cells) == (
        None,
        "explicit",
        2,
        ("设备", "端口"),
    )


@pytest.mark.asyncio
async def test_preview_of_empty_source_fails_without_returning_placeholder_chunks(tmp_path: Path) -> None:
    from actweave_knowledge.ingestion.preview import preview_document_chunks

    source = tmp_path / "empty.txt"
    source.write_text("   \n\n", encoding="utf-8")
    settings = KnowledgeSettings()
    capabilities = build_file_capabilities(settings, default_registry())

    with pytest.raises(KnowledgeError) as caught:
        await preview_document_chunks(
            _request(source),
            settings,
            capability_revision=capabilities.capability_revision,
            parser_slots=ParserSlots(1),
            guard=_guard,
            registry=default_registry(),
        )
    assert caught.value.code == "KNOWLEDGE_PARSE_FAILED"


@pytest.mark.asyncio
async def test_preview_parser_slot_is_nonqueueing(tmp_path: Path) -> None:
    from actweave_knowledge.ingestion.preview import preview_document_chunks

    source = tmp_path / "busy.txt"
    source.write_text("正文", encoding="utf-8")
    settings = KnowledgeSettings()
    capabilities = build_file_capabilities(settings, default_registry())
    slots = ParserSlots(1)

    async with slots:
        with pytest.raises(ExtractionError) as caught:
            await preview_document_chunks(
                _request(source),
                settings,
                capability_revision=capabilities.capability_revision,
                parser_slots=slots,
                guard=_guard,
                registry=default_registry(),
            )
    assert caught.value.reason_code == "PARSER_BUSY"


@pytest.mark.asyncio
async def test_preview_rejects_non_snapshot_capability_revision_before_parser_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from actweave_knowledge.ingestion import preview as preview_module

    source = tmp_path / "revision.txt"
    source.write_text("正文", encoding="utf-8")
    parser_started = False

    async def run_extraction(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal parser_started
        parser_started = True
        raise AssertionError("invalid process snapshot must not start the parser")

    monkeypatch.setattr(preview_module, "run_extraction", run_extraction)
    with pytest.raises(ValueError, match="capability revision"):
        await preview_module.preview_document_chunks(
            _request(source),
            KnowledgeSettings(),
            capability_revision="not-a-process-snapshot",
            parser_slots=ParserSlots(1),
            guard=_guard,
            registry=default_registry(),
        )
    assert parser_started is False


@pytest.mark.asyncio
async def test_preview_revalidates_after_runner_before_returning_result(tmp_path: Path) -> None:
    from actweave_knowledge.ingestion.preview import preview_document_chunks

    source = tmp_path / "revoked.txt"
    source.write_text("正文", encoding="utf-8")
    settings = KnowledgeSettings()
    capabilities = build_file_capabilities(settings, default_registry())
    calls = 0
    revoked = RuntimeError("authority revoked")

    async def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise revoked

    with pytest.raises(RuntimeError) as caught:
        await preview_document_chunks(
            _request(source),
            settings,
            capability_revision=capabilities.capability_revision,
            parser_slots=ParserSlots(1),
            guard=guard,
            registry=default_registry(),
        )
    assert caught.value is revoked


@pytest.mark.asyncio
async def test_preview_cancellation_waits_for_runner_cleanup_before_removing_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from actweave_knowledge.ingestion import preview as preview_module

    source = tmp_path / "cancel.txt"
    source.write_text("正文", encoding="utf-8")
    settings = KnowledgeSettings()
    capabilities = build_file_capabilities(settings, default_registry())
    started = asyncio.Event()
    release = asyncio.Event()
    runner_settled = asyncio.Event()
    observed_work_dir: Path | None = None

    async def run_extraction(setting, *, work_dir, limits, timeout_seconds, on_asset, guard):  # noqa: ANN001
        del setting, limits, timeout_seconds, on_asset, guard
        nonlocal observed_work_dir
        observed_work_dir = work_dir
        started.set()
        waiter = asyncio.create_task(release.wait())
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            await waiter
            raise
        finally:
            assert work_dir.exists()
            runner_settled.set()
        raise AssertionError("cancelled runner must not return")

    monkeypatch.setattr(preview_module, "run_extraction", run_extraction)
    task = asyncio.create_task(
        preview_module.preview_document_chunks(
            _request(source),
            settings,
            capability_revision=capabilities.capability_revision,
            parser_slots=ParserSlots(1),
            guard=_guard,
            registry=default_registry(),
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert runner_settled.is_set()
    assert observed_work_dir is not None and not observed_work_dir.exists()


@pytest.mark.asyncio
async def test_only_first_ten_parent_refs_can_select_preview_thumbnails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from actweave_knowledge.ingestion import preview as preview_module

    source = tmp_path / "parents.txt"
    source.write_text("fixture", encoding="utf-8")
    work_assets = tmp_path / "fixture-assets"
    work_assets.mkdir()
    assets = [_asset(work_assets, color=(index, index, index)) for index in range(11)]
    documents = []
    for index, asset in enumerate(assets, 1):
        text = f"第{index}段正文 " + (chr(64 + index) * 180)
        image = f"![图{index}](knowledge-attachment:{asset.attachment.ref})"
        content = text + image
        occurrence = AttachmentOccurrence(
            ref=asset.attachment.ref,
            alt_text=f"图{index}",
            source=SourceSpan(
                block_id=f"block:{index}:image",
                start=len(text),
                end=len(content),
                location={"paragraph": index, "image_index": 1},
            ),
        )
        documents.append(
            Document(
                page_content=content,
                source_spans=(SourceSpan(block_id=f"block:{index}", start=0, end=len(text), location={"paragraph": index}),),
                heading_path=(f"第{index}节",),
                attachments=(occurrence,),
            )
        )
    profile = make_parse_profile(".txt")
    result = ExtractionResult(
        documents=tuple(documents),
        attachments=tuple(asset.attachment for asset in assets),
        warnings=(ParseWarning(code="FIXTURE", message="fixture warning"),),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        parse_fingerprint=canonical_parse_fingerprint(profile),
    )

    async def run_extraction(setting, *, work_dir, limits: ExtractionLimits, timeout_seconds, on_asset, guard):  # noqa: ANN001
        del setting, limits
        assert timeout_seconds == 120
        for asset in assets:
            target = work_dir / asset.relative_path
            target.write_bytes((work_assets / asset.relative_path).read_bytes())
            await guard()
            await on_asset(asset)
        return result

    monkeypatch.setattr(preview_module, "run_extraction", run_extraction)
    settings = KnowledgeSettings()
    capabilities = build_file_capabilities(settings, default_registry())
    preview = await preview_module.preview_document_chunks(
        _request(source, processing_profile=ProcessingParameters(size=200, overlap=0, child_size=100)),
        settings,
        capability_revision=capabilities.capability_revision,
        parser_slots=ParserSlots(1),
        guard=_guard,
        registry=default_registry(),
    )

    assert preview.total == 11
    assert [item.ref for item in preview.preview_attachments] == [asset.attachment.ref for asset in assets[:10]]
    assert preview.omitted_preview_attachment_count == 0
    assert [warning.code for warning in preview.warnings] == ["FIXTURE"]
