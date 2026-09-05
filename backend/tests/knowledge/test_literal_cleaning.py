"""Optional cleaning must understand the literal serializer's output."""

import pytest
from actweave_knowledge.extraction.literal import escape_literal_text
from actweave_knowledge.ingestion.cleaner import clean_documents
from actweave_knowledge.ingestion.index_text import build_index_text
from parsing_test_helpers import make_document


@pytest.mark.parametrize("email", ["a_b@example.test", "+alice@example.test", ".alice@example.test", "a.b+c_d@example-domain.test"])
def test_email_removal_consumes_the_whole_escaped_address(email):
    rendered = escape_literal_text(email)
    source = make_document("before " + rendered + " after")
    assert clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=False) == (source,)
    (cleaned,) = clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=True)
    assert cleaned.page_content == "before  after"
    assert build_index_text(cleaned.page_content) == "before  after"


def test_markdown_email_pattern_does_not_change_raw_or_character_cleaning():
    from actweave_knowledge.ingestion.cleaner import clean_character_document, clean_text

    raw = "a_b@example.test"
    assert clean_text(raw, remove_extra_spaces=False, remove_urls_emails=True) == ""
    assert clean_character_document(make_document(raw), remove_extra_spaces=False, remove_urls_emails=True).page_content == ""
    historical_source = r"a\_b@example.test"
    assert clean_text(historical_source, remove_extra_spaces=False, remove_urls_emails=True) == "a\\"
    assert clean_character_document(make_document(historical_source), remove_extra_spaces=False, remove_urls_emails=True).page_content == "a\\"


def test_email_cleaning_remaps_surviving_sources_and_attachment_exactly():
    from actweave_knowledge.extraction.contracts import AttachmentOccurrence, Document, SourceSpan

    email = escape_literal_text("a_b@example.test")
    ref = "a" * 64
    image = f"![image](knowledge-attachment:{ref})"
    left, right = "before ", " after "
    prefix = left + email + right
    image_span = SourceSpan(block_id="image", start=len(prefix), end=len(prefix) + len(image), location={"paragraph": 2})
    source = Document(
        page_content=prefix + image,
        source_spans=(
            SourceSpan(block_id="before", start=0, end=len(left), location={"paragraph": 1}),
            SourceSpan(block_id="email", start=len(left), end=len(left) + len(email), location={"paragraph": 1}),
            SourceSpan(block_id="after", start=len(left) + len(email), end=len(prefix), location={"paragraph": 1}),
            image_span,
        ),
        attachments=(AttachmentOccurrence(ref=ref, alt_text="image", source=image_span),),
    )
    (cleaned,) = clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=True)
    assert cleaned.page_content == left + right + image
    assert [(span.block_id, cleaned.page_content[span.start : span.end]) for span in cleaned.source_spans] == [
        ("before", left),
        ("after", right),
        ("image", image),
    ]
    (occurrence,) = cleaned.attachments
    assert occurrence.source.start == len(left + right)
    assert cleaned.page_content[occurrence.source.start : occurrence.source.end] == image
    assert occurrence.source.location == {"paragraph": 2}


def test_email_cleaning_keeps_real_code_and_existing_link_label_rule():
    source = make_document("`a_b@example.test`\n\n```text\n+alice@example.test\n```\n\n[label](https://example.invalid/path)\n\n" + escape_literal_text("https://example.invalid/a_b"))
    (cleaned,) = clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=True)
    assert "`a_b@example.test`" in cleaned.page_content
    assert "```text\n+alice@example.test\n```" in cleaned.page_content
    assert "label" in cleaned.page_content
    assert "https://example.invalid" not in cleaned.page_content


@pytest.mark.parametrize("indent", ["    ", "\t  ", " \t", " \t   "])
def test_extra_space_cleaning_counts_generated_indentation_entities(indent):
    source = make_document(escape_literal_text(indent + "# text"))
    (cleaned,) = clean_documents((source,), remove_extra_spaces=True, remove_urls_emails=False)
    assert cleaned.page_content == " \\# text"
    assert "".join(cleaned.page_content[s.start : s.end] for s in cleaned.source_spans) == cleaned.page_content
    assert all(span.location == {"paragraph": 1} for span in cleaned.source_spans)


def test_space_cleaning_distinguishes_source_entity_text_and_preserves_code():
    source = make_document(escape_literal_text("&#32;  # literal"))
    (cleaned,) = clean_documents((source,), remove_extra_spaces=True, remove_urls_emails=False)
    assert cleaned.page_content == escape_literal_text("&#32; # literal")
    assert build_index_text(cleaned.page_content) == "&#32; # literal"
    code = make_document("`&#32;   x`\n\n```text\n&#9;   y\n```")
    assert clean_documents((code,), remove_extra_spaces=True, remove_urls_emails=False) == (code,)
    disabled = make_document(escape_literal_text("    # unchanged"))
    assert clean_documents((disabled,), remove_extra_spaces=False, remove_urls_emails=False) == (disabled,)
