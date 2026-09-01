"""CSV document loader adapted from upstream's pinned CSVExtractor.

Upstream: api/core/rag/extractor/csv_extractor.py at
9c16c865977e9d89a9ec7ae0536e893f4385a758. See UPSTREAM.md and patches.md.
The extract -> _read_from_file -> row documents flow is retained; strict csv
records replace pandas inference/skip behavior to preserve every source field.
"""

from __future__ import annotations

import csv
import io
from typing import TextIO, override

from ..base import BaseExtractor
from ..contracts import Document, ExtractionContext, ExtractionError, ExtractSetting, HeaderRule
from ..encoding import decode_text_file
from ..tabular import TableRow, header_record_index, rows_to_documents


def read_csv_rows(text: str) -> list[TableRow]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[TableRow] = []
    previous_end = 0
    # Parsing runs in its isolated child; restore the process-wide csv setting.
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(max(previous_limit, len(text)))
    try:
        for row in reader:
            start, end = previous_end + 1, reader.line_num
            previous_end = end
            rows.append((start, end, list(row)))
    except csv.Error:
        raise ExtractionError("CSV_ROW_INVALID", "CSV 引号或字段格式无效") from None
    finally:
        csv.field_size_limit(previous_limit)
    return rows


def validate_csv_width(rows: list[TableRow], header_index: int | None) -> None:
    """Check from a one-based logical header record, preserving preceding notes."""
    begin = header_index - 1 if header_index is not None else 0
    records = [record for record in rows[begin:] if record[2]]
    if records and any(len(record[2]) != len(records[0][2]) for record in records):
        raise ExtractionError("CSV_ROW_INVALID", "CSV 行列数不一致")


class CSVExtractor(BaseExtractor):
    """Load CSV files into field-bound row documents with physical locations."""

    @override
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        """Load data into document objects using the shared strict decoder."""
        context.check_cancelled()
        text, encoding, warnings = decode_text_file(setting.source_path)
        rule = next((rule for rule in setting.profile.header_rules if rule.sheet is None), HeaderRule())
        docs = self._read_from_file(io.StringIO(text, newline=""), rule)
        result = []
        for doc in docs:
            context.check_cancelled()
            spans = tuple(span.model_copy(update={"location": {**span.location, "encoding": encoding}}) for span in doc.source_spans)
            result.append(doc.model_copy(update={"source_spans": spans, "warnings": doc.warnings + warnings}))
        return result

    def _read_from_file(self, csvfile: TextIO, rule: HeaderRule) -> list[Document]:
        rows = read_csv_rows(csvfile.read())
        header_index = header_record_index(rows, rule)
        validate_csv_width(rows, header_index + 1 if header_index is not None else None)
        return rows_to_documents(rows, sheet=None, rule=rule)
