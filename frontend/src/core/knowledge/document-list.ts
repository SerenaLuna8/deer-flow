import type { KnowledgeDocumentSort } from "./navigation";
import type { KnowledgeDocumentItem, KnowledgeDocumentStatus } from "./types";

/**
 * Pure filter → sort → paginate pipeline for the document list.
 *
 * It runs over the complete authoritative list (the API layer pages to
 * completion and fails loudly when it cannot), so filters see every row —
 * never just the first backend page. The keyword is transient UI state and
 * must not be persisted to the URL or browser storage by callers.
 */
export type KnowledgeDocumentListQuery = {
  /** Case-insensitive substring over name and original_name; "" disables. */
  keyword: string;
  status: KnowledgeDocumentStatus | null;
  sort: KnowledgeDocumentSort;
  /** Requested 1-based page; the result clamps it to a legal page. */
  page: number;
};

export type KnowledgeDocumentListView = {
  /** Rows of the effective page, in display order. */
  rows: KnowledgeDocumentItem[];
  filteredTotal: number;
  /** Always >= 1 so "page 1 of 1" renders even when empty. */
  pageCount: number;
  /** The effective page after clamping (deleting the last page steps back). */
  page: number;
};

export const KNOWLEDGE_DOCUMENT_LIST_PAGE_SIZE = 20;

type Comparator = (
  a: KnowledgeDocumentItem,
  b: KnowledgeDocumentItem,
) => number;

function compareCreated(a: KnowledgeDocumentItem, b: KnowledgeDocumentItem) {
  return a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0;
}

function compareName(a: KnowledgeDocumentItem, b: KnowledgeDocumentItem) {
  return a.name.localeCompare(b.name, "zh-CN");
}

const COMPARATORS: Record<KnowledgeDocumentSort, Comparator> = {
  created_desc: (a, b) => compareCreated(b, a),
  created_asc: compareCreated,
  name_asc: compareName,
  name_desc: (a, b) => compareName(b, a),
};

export function deriveKnowledgeDocumentList(
  items: readonly KnowledgeDocumentItem[],
  query: KnowledgeDocumentListQuery,
): KnowledgeDocumentListView {
  const keyword = query.keyword.trim().toLowerCase();
  const comparator = COMPARATORS[query.sort];

  const filtered = items
    .filter((item) => {
      if (query.status !== null && item.status !== query.status) return false;
      if (keyword.length === 0) return true;
      return (
        item.name.toLowerCase().includes(keyword) ||
        item.original_name.toLowerCase().includes(keyword)
      );
    })
    // Ties break on the immutable id so equal keys keep one stable order
    // regardless of the input order.
    .sort((a, b) => comparator(a, b) || a.id.localeCompare(b.id));

  const pageCount = Math.max(
    1,
    Math.ceil(filtered.length / KNOWLEDGE_DOCUMENT_LIST_PAGE_SIZE),
  );
  const page = Math.min(Math.max(1, query.page), pageCount);
  const start = (page - 1) * KNOWLEDGE_DOCUMENT_LIST_PAGE_SIZE;
  return {
    rows: filtered.slice(start, start + KNOWLEDGE_DOCUMENT_LIST_PAGE_SIZE),
    filteredTotal: filtered.length,
    pageCount,
    page,
  };
}
