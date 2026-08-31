import type { KnowledgeDocumentStatus } from "./types";

/**
 * URL state for the project Knowledge workspace.
 *
 * The query string carries only this whitelist — `kb/view/doc/segment/
 * status/sort/page` — all syntactically validated here. Free-text state
 * (file-name keywords, search queries, metadata values) must never enter the
 * URL or persistent browser storage; it lives in non-persistent UI state.
 *
 * The URL is navigation, not authorization: resolving an id to an accessible
 * resource (or an "inaccessible" notice) is the caller's job.
 */
export type KnowledgeView = "documents" | "search" | "metadata" | "settings";

export type KnowledgeDocumentSort =
  | "created_desc"
  | "created_asc"
  | "name_asc"
  | "name_desc";

export type KnowledgeNavigationState = {
  /** Selected knowledge base id, or null for the base list. */
  kb: string | null;
  view: KnowledgeView;
  /** Document opened in the segments browser (documents view only). */
  doc: string | null;
  /** Segment to locate via its detail endpoint (requires doc). */
  segment: string | null;
  /** Document-list lifecycle filter; null shows every status. */
  status: KnowledgeDocumentStatus | null;
  sort: KnowledgeDocumentSort;
  /** 1-based document-list page. */
  page: number;
};

export const KNOWLEDGE_DEFAULT_SORT: KnowledgeDocumentSort = "created_desc";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const VIEWS: readonly KnowledgeView[] = [
  "documents",
  "search",
  "metadata",
  "settings",
];

const STATUSES: readonly KnowledgeDocumentStatus[] = [
  "uploading",
  "queued",
  "processing",
  "ready",
  "failed",
  "deleting",
];

const SORTS: readonly KnowledgeDocumentSort[] = [
  "created_desc",
  "created_asc",
  "name_asc",
  "name_desc",
];

/** Bounded so absurd page numbers cannot drive oversized offsets. */
const MAX_PAGE = 10_000;

function parseUuid(value: string | null): string | null {
  if (value === null) return null;
  const normalized = value.toLowerCase();
  return UUID_RE.test(normalized) ? normalized : null;
}

function parseEnum<T extends string>(
  value: string | null,
  allowed: readonly T[],
): T | null {
  return value !== null && (allowed as readonly string[]).includes(value)
    ? (value as T)
    : null;
}

function parsePage(value: string | null): number {
  if (value === null || !/^\d+$/.test(value)) return 1;
  const page = Number.parseInt(value, 10);
  return page >= 1 && page <= MAX_PAGE ? page : 1;
}

export function parseKnowledgeNavigation(
  params: Pick<URLSearchParams, "get">,
): KnowledgeNavigationState {
  const kb = parseUuid(params.get("kb"));
  // Every other field describes state inside one base; without a valid kb
  // they would point nowhere, so they reset rather than dangle.
  if (kb === null) {
    return {
      kb: null,
      view: "documents",
      doc: null,
      segment: null,
      status: null,
      sort: KNOWLEDGE_DEFAULT_SORT,
      page: 1,
    };
  }

  const view = parseEnum(params.get("view"), VIEWS) ?? "documents";
  if (view !== "documents") {
    // doc/segment/status/sort/page are document-list state; other views
    // have none, and stale copies must not resurrect on the way back.
    return {
      kb,
      view,
      doc: null,
      segment: null,
      status: null,
      sort: KNOWLEDGE_DEFAULT_SORT,
      page: 1,
    };
  }

  const doc = parseUuid(params.get("doc"));
  return {
    kb,
    view,
    doc,
    // A segment is located within an open document's detail; alone it
    // cannot be resolved without scanning the whole base.
    segment: doc === null ? null : parseUuid(params.get("segment")),
    status: parseEnum(params.get("status"), STATUSES),
    sort: parseEnum(params.get("sort"), SORTS) ?? KNOWLEDGE_DEFAULT_SORT,
    page: parsePage(params.get("page")),
  };
}

/**
 * Builds the query string (`""` or `"?..."`) for a navigation state.
 * Default values are omitted so canonical states share canonical URLs, and
 * the field order is fixed to keep URLs comparable in history and tests.
 */
export function buildKnowledgeSearch(state: KnowledgeNavigationState): string {
  const params = new URLSearchParams();
  if (state.kb === null) return "";
  params.set("kb", state.kb);
  if (state.view !== "documents") {
    params.set("view", state.view);
    return `?${params.toString()}`;
  }
  if (state.doc !== null) {
    params.set("doc", state.doc);
    if (state.segment !== null) params.set("segment", state.segment);
  }
  if (state.status !== null) params.set("status", state.status);
  if (state.sort !== KNOWLEDGE_DEFAULT_SORT) params.set("sort", state.sort);
  if (state.page !== 1) params.set("page", String(state.page));
  const query = params.toString();
  return query.length > 0 ? `?${query}` : "";
}
