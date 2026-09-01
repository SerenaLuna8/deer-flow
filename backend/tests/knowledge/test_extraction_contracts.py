from __future__ import annotations

import json

import pytest
from actweave_knowledge.contracts import (
    KNOWLEDGE_PARSE_FAILED,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KnowledgeError,
)
from actweave_knowledge.extraction.contracts import (
    Attachment,
    AttachmentOccurrence,
    Document,
    ExtractionError,
    ExtractionLimits,
    ExtractionResult,
    HeaderRule,
    ParseProfile,
    SourceSpan,
)
from actweave_knowledge.extraction.manifest import (
    canonical_parse_fingerprint,
    decode_manifest,
    encode_manifest,
)
from pydantic import ValidationError


def _profile() -> ParseProfile:
    return ParseProfile(
        etl_type="builtin",
        extractor_id="builtin.pdf",
        extractor_version="upstream-adapter-build",
        normalization_version="md-v1",
        image_policy_version="raster-v1",
    )


def _result(*, documents: tuple[Document, ...], attachments: tuple[Attachment, ...] = ()) -> ExtractionResult:
    return ExtractionResult(
        documents=documents,
        attachments=attachments,
        source_sha256="a" * 64,
        parse_fingerprint=canonical_parse_fingerprint(_profile()),
    )


def _attachment(ref: str = "b" * 64) -> Attachment:
    return Attachment(
        ref=ref,
        media_type="image/png",
        size_bytes=4,
        width=2,
        height=2,
    )


def _image_link(ref: str) -> str:
    return f"![diagram](knowledge-attachment:{ref})"


def _occurrence(ref: str, start: int, end: int) -> AttachmentOccurrence:
    return AttachmentOccurrence(
        ref=ref,
        alt_text="diagram",
        source=SourceSpan(block_id="p:1", start=start, end=end, location={"paragraph": 1}),
    )


def test_manifest_roundtrip_is_canonical_and_excludes_local_paths() -> None:
    documents = tuple(
        Document(
            page_content=text,
            source_spans=(SourceSpan(block_id=f"page:{number}", start=0, end=len(text), location={"page": number}),),
            kind="page",
        )
        for number, text in enumerate(("第一页", "第二页"), 1)
    )
    result = _result(documents=documents)

    payload = encode_manifest(result)

    assert decode_manifest(payload, ExtractionLimits()) == result
    assert payload == encode_manifest(decode_manifest(payload, ExtractionLimits()))
    assert [document.source_spans[0].location["page"] for document in result.documents] == [1, 2]
    assert b"relative_path" not in payload
    assert b"source_path" not in payload


def test_manifest_rejects_unknown_fields_and_invalid_versions_without_partial_result() -> None:
    result = _result(documents=(Document(page_content="content"),))
    envelope = json.loads(encode_manifest(result))
    envelope["result"]["source_path"] = "/private/source.pdf"

    with pytest.raises(ExtractionError) as unknown_field:
        decode_manifest(json.dumps(envelope).encode(), ExtractionLimits())
    assert unknown_field.value.code == KNOWLEDGE_PARSE_FAILED
    assert unknown_field.value.reason_code == "INVALID_MANIFEST"

    with pytest.raises(ExtractionError) as unavailable_profile:
        decode_manifest(b'{"format_version":2,"result":{}}', ExtractionLimits())
    assert unavailable_profile.value.code == KNOWLEDGE_PARSE_FAILED
    assert unavailable_profile.value.reason_code == "PARSER_PROFILE_UNAVAILABLE"


@pytest.mark.parametrize("version", [True, 1.0])
def test_manifest_rejects_non_integer_format_versions(version: object) -> None:
    result = _result(documents=(Document(page_content="content"),))
    envelope = json.loads(encode_manifest(result))
    envelope["format_version"] = version

    with pytest.raises(ExtractionError) as invalid_version:
        decode_manifest(json.dumps(envelope).encode(), ExtractionLimits())
    assert invalid_version.value.reason_code == "INVALID_MANIFEST"


def test_manifest_enforces_supplied_budgets_and_attachment_references() -> None:
    attachment = _attachment()
    link = _image_link(attachment.ref)
    document = Document(
        page_content=link,
        attachments=(_occurrence(attachment.ref, 0, len(link)),),
    )
    result = _result(documents=(document,), attachments=(attachment,))

    with pytest.raises(KnowledgeError) as text_limit:
        decode_manifest(encode_manifest(result), ExtractionLimits(max_text_chars=len(link) - 1))
    assert text_limit.value.code == KNOWLEDGE_QUOTA_EXCEEDED

    unreferenced = _result(documents=(Document(page_content="text"),), attachments=(attachment,))
    with pytest.raises(KnowledgeError) as unreferenced_asset:
        encode_manifest(unreferenced)
    assert unreferenced_asset.value.code == KNOWLEDGE_PARSE_FAILED

    missing_asset_link = _image_link("c" * 64)
    missing_asset = _result(
        documents=(
            Document(
                page_content=missing_asset_link,
                attachments=(_occurrence("c" * 64, 0, len(missing_asset_link)),),
            ),
        ),
    )
    with pytest.raises(KnowledgeError) as missing_reference:
        encode_manifest(missing_asset)
    assert missing_reference.value.code == KNOWLEDGE_PARSE_FAILED


def test_manifest_rejects_logical_image_without_asset_or_occurrence() -> None:
    link = _image_link("b" * 64)

    with pytest.raises(ExtractionError) as invalid_manifest:
        encode_manifest(_result(documents=(Document(page_content=link),)))

    assert invalid_manifest.value.reason_code == "INVALID_MANIFEST"


def test_manifest_rejects_asset_occurrence_without_rendered_logical_image() -> None:
    attachment = _attachment()
    document = Document(
        page_content="plain text",
        attachments=(_occurrence(attachment.ref, 0, 0),),
    )

    with pytest.raises(ExtractionError) as invalid_manifest:
        encode_manifest(_result(documents=(document,), attachments=(attachment,)))

    assert invalid_manifest.value.reason_code == "INVALID_MANIFEST"


def test_manifest_rejects_logical_image_ref_that_differs_from_occurrence_and_asset() -> None:
    attachment = _attachment()
    link = _image_link("c" * 64)
    document = Document(
        page_content=link,
        attachments=(_occurrence(attachment.ref, 0, len(link)),),
    )

    with pytest.raises(ExtractionError) as invalid_manifest:
        encode_manifest(_result(documents=(document,), attachments=(attachment,)))

    assert invalid_manifest.value.reason_code == "INVALID_MANIFEST"


def test_manifest_keeps_duplicate_rendered_image_occurrences_and_ignores_literals() -> None:
    attachment = _attachment()
    link = _image_link(attachment.ref)
    prefix = f"`{link}`\n\n```markdown\n{link}\n```\n\n\\{link}\n\n"
    content = prefix + link + "\n\n" + link
    first_start = len(prefix)
    second_start = first_start + len(link) + 2
    document = Document(
        page_content=content,
        attachments=(
            _occurrence(attachment.ref, first_start, first_start + len(link)),
            _occurrence(attachment.ref, second_start, second_start + len(link)),
        ),
    )
    result = _result(documents=(document,), attachments=(attachment,))

    assert decode_manifest(encode_manifest(result), ExtractionLimits()) == result


def test_manifest_keeps_rendered_image_offset_after_escaped_backtick() -> None:
    attachment = _attachment()
    link = _image_link(attachment.ref)
    content = "\\`" + link + "` " + link + "`"
    rendered_start = 2
    document = Document(
        page_content=content,
        attachments=(_occurrence(attachment.ref, rendered_start, rendered_start + len(link)),),
    )
    result = _result(documents=(document,), attachments=(attachment,))

    assert decode_manifest(encode_manifest(result), ExtractionLimits()) == result


def test_manifest_rejects_code_example_offset_after_escaped_backtick() -> None:
    attachment = _attachment()
    link = _image_link(attachment.ref)
    content = "\\`" + link + "` " + link + "`"
    code_example_start = 2 + len(link) + 2
    document = Document(
        page_content=content,
        attachments=(_occurrence(attachment.ref, code_example_start, code_example_start + len(link)),),
    )

    with pytest.raises(ExtractionError) as invalid_manifest:
        encode_manifest(_result(documents=(document,), attachments=(attachment,)))

    assert invalid_manifest.value.reason_code == "INVALID_MANIFEST"


def test_manifest_keeps_rendered_image_after_unmatched_unequal_backtick_runs() -> None:
    attachment = _attachment()
    link = _image_link(attachment.ref)
    content = "`` " + link + " ```"
    rendered_start = 3
    document = Document(
        page_content=content,
        attachments=(_occurrence(attachment.ref, rendered_start, rendered_start + len(link)),),
    )
    result = _result(documents=(document,), attachments=(attachment,))

    assert decode_manifest(encode_manifest(result), ExtractionLimits()) == result


def test_manifest_keeps_only_real_image_after_equal_length_inline_code() -> None:
    attachment = _attachment()
    link = _image_link(attachment.ref)
    content = "``" + link + "`` " + link
    rendered_start = len(link) + 5
    document = Document(
        page_content=content,
        attachments=(_occurrence(attachment.ref, rendered_start, rendered_start + len(link)),),
    )
    result = _result(documents=(document,), attachments=(attachment,))

    assert decode_manifest(encode_manifest(result), ExtractionLimits()) == result


def test_manifest_rejects_duplicate_asset_refs() -> None:
    attachment = _attachment()
    link = _image_link(attachment.ref)
    document = Document(page_content=link, attachments=(_occurrence(attachment.ref, 0, len(link)),))

    with pytest.raises(ExtractionError) as duplicate_refs:
        encode_manifest(_result(documents=(document,), attachments=(attachment, attachment)))

    assert duplicate_refs.value.reason_code == "INVALID_MANIFEST"


def test_contracts_reject_unsafe_metadata_and_invalid_bounds() -> None:
    with pytest.raises(ValidationError):
        Document(page_content="x", project_id="untrusted")
    with pytest.raises(ValidationError):
        Document(
            page_content="x",
            source_spans=(SourceSpan(block_id="p:1", start=0, end=2, location={"paragraph": 1}),),
        )
    with pytest.raises(ValidationError):
        SourceSpan(block_id="p:1", start=0, end=1, location={"source_path": "/private/file"})
    with pytest.raises(ValidationError):
        HeaderRule(mode="explicit")
    with pytest.raises(ValidationError):
        HeaderRule(mode="auto", row=1)
    with pytest.raises(ValidationError):
        ExtractionLimits(max_text_chars=0)
    with pytest.raises(ValidationError):
        ExtractionLimits(max_text_chars=-1)
    with pytest.raises(ValidationError):
        SourceSpan(block_id="p:1", start=0, end=1, location={"page": 0})


@pytest.mark.parametrize(
    "template",
    [
        '{{"{link}"}}\n',
        '<Example value={{"{link}"}} />\n',
        "<pre>\n{link}\n</pre>\n",
    ],
)
def test_manifest_and_normalizer_agree_on_inert_mdx_and_html_images(template):
    from actweave_knowledge.extraction.normalizer import normalize_documents

    content = template.format(link=_image_link("b" * 64))
    doc = Document(page_content=content)
    assert normalize_documents([doc]) == [doc]
    result = _result(documents=(doc,))
    assert decode_manifest(encode_manifest(result), ExtractionLimits()) == result


def test_manifest_and_normalizer_agree_on_image_between_unmatched_backtick_paragraphs():
    from actweave_knowledge.extraction.normalizer import normalize_documents

    asset = _attachment()
    link = _image_link(asset.ref)
    prefix = "`unfinished\n\n"
    content = prefix + link + "\n\n`\n"
    doc = Document(page_content=content, attachments=(_occurrence(asset.ref, len(prefix), len(prefix) + len(link)),))
    assert normalize_documents([doc]) == [doc]
    result = _result(documents=(doc,), attachments=(asset,))
    assert decode_manifest(encode_manifest(result), ExtractionLimits()) == result
    with pytest.raises(ExtractionError) as invalid:
        encode_manifest(_result(documents=(Document(page_content=content),)))
    assert invalid.value.reason_code == "INVALID_MANIFEST"


def test_manifest_rejects_literal_mdx_occurrence_even_when_ref_inventory_match():
    asset = _attachment()
    link = _image_link(asset.ref)
    content = '{"' + link + '"}'
    doc = Document(page_content=content, attachments=(_occurrence(asset.ref, 2, 2 + len(link)),))
    with pytest.raises(ExtractionError) as invalid:
        encode_manifest(_result(documents=(doc,), attachments=(asset,)))
    assert invalid.value.reason_code == "INVALID_MANIFEST"


@pytest.mark.parametrize(
    "syntax",
    [
        "![diagram](<knowledge-attachment:{ref}>)",
        '![diagram](knowledge-attachment:{ref} "title")',
        "![diagram][image]\n\n[image]: knowledge-attachment:{ref}",
    ],
)
def test_manifest_still_rejects_noncanonical_logical_image_syntax(syntax):
    asset = _attachment()
    content = syntax.format(ref=asset.ref)
    doc = Document(page_content=content, attachments=(_occurrence(asset.ref, 0, len(content)),))
    with pytest.raises(ExtractionError) as invalid:
        encode_manifest(_result(documents=(doc,), attachments=(asset,)))
    assert invalid.value.reason_code == "INVALID_MANIFEST"
