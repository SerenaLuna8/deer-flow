/**
 * Human-readable rendering of a segment's `source_position`.
 *
 * The ingestion extractor records provenance per source format — `{page}` for
 * PDF, `{paragraph}` or `{table, row}` for DOCX, `{row}` for CSV, `{sheet, row}` for XLSX,
 * `{slide}` for PPTX, `{chapter}` for EPUB, and `{}` for plain text and HTML.
 * The formatter is deliberately closed over these known keys: an unknown
 * payload renders as nothing rather than leaking raw JSON.
 */

export type KnowledgeSourcePositionLabels = {
  page: (page: string) => string;
  paragraph: (paragraph: string) => string;
  table: (table: string) => string;
  row: (row: string) => string;
  slide: (slide: string) => string;
  chapter: (chapter: string) => string;
};

function scalarText(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  if (typeof value === "string" && value.length > 0) {
    return value;
  }
  return null;
}

export function formatKnowledgeSourcePosition(
  position: Record<string, unknown>,
  labels: KnowledgeSourcePositionLabels,
): string | null {
  const page = scalarText(position.page);
  if (page !== null) return labels.page(page);
  const paragraph = scalarText(position.paragraph);
  if (paragraph !== null) return labels.paragraph(paragraph);
  const slide = scalarText(position.slide);
  if (slide !== null) return labels.slide(slide);
  const chapter = scalarText(position.chapter);
  if (chapter !== null) return labels.chapter(chapter);
  const row = scalarText(position.row);
  const table = scalarText(position.table);
  if (table !== null && row !== null) {
    return `${labels.table(table)} · ${labels.row(row)}`;
  }
  const sheet = scalarText(position.sheet);
  if (sheet !== null && row !== null) {
    return `${sheet} · ${labels.row(row)}`;
  }
  if (row !== null) return labels.row(row);
  return null;
}

export type KnowledgeSourceSpanPosition = Readonly<{
  location: Record<string, unknown>;
  role: "source" | "context_prefix";
}>;

export type FormattedKnowledgeSourceSpan = Readonly<{
  position: string;
  role: KnowledgeSourceSpanPosition["role"];
}>;

/** Preserve every verifiable source while ignoring metadata-only locations. */
export function formatKnowledgeSourceSpans(
  spans: readonly KnowledgeSourceSpanPosition[],
  labels: KnowledgeSourcePositionLabels,
): FormattedKnowledgeSourceSpan[] {
  return spans.flatMap((span) => {
    const position = formatKnowledgeSourcePosition(span.location, labels);
    return position === null ? [] : [{ position, role: span.role }];
  });
}
