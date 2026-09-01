import { describe, expect, test } from "@rstest/core";

import {
  deriveKnowledgeDocumentList,
  KNOWLEDGE_DOCUMENT_LIST_PAGE_SIZE,
} from "@/core/knowledge/document-list";
import type { KnowledgeDocumentItem } from "@/core/knowledge/types";

function doc(
  index: number,
  overrides: Partial<KnowledgeDocumentItem> = {},
): KnowledgeDocumentItem {
  const suffix = String(index).padStart(12, "0");
  return {
    parsing_profile: null,
    parse_warnings: [],
    chunk_size_unit: "character",
    tokenizer_profile_id: null,
    content_initialized: true,
    id: `50000000-0000-4000-8000-${suffix}`,
    project_id: "10000000-0000-4000-8000-000000000001",
    knowledge_base_id: "40000000-0000-4000-8000-000000000001",
    name: `文档 ${String(index).padStart(3, "0")}`,
    original_name: `file-${String(index).padStart(3, "0")}.pdf`,
    media_type: "application/pdf",
    size_bytes: 1024,
    status: "ready",
    enabled: true,
    version: 1,
    chunk_size: 1000,
    chunk_overlap: 100,
    chunk_separator: "\\n\\n",
    remove_extra_spaces: false,
    remove_urls_emails: false,
    chunking_mode: "general",
    child_chunk_size: 500,
    child_chunk_separator: "\\n",
    segment_count: 3,
    word_count: 100,
    hit_count: 0,
    doc_metadata: {},
    error_message: null,
    delete_error: null,
    task_progress: null,
    created_at: `2026-08-${String((index % 28) + 1).padStart(2, "0")}T00:00:00Z`,
    updated_at: "2026-08-30T00:00:00Z",
    ...overrides,
  };
}

const QUERY = {
  keyword: "",
  status: null,
  sort: "created_desc",
  page: 1,
} as const;

describe("deriveKnowledgeDocumentList", () => {
  test("filters by keyword over name and original name, case-insensitively", () => {
    const items = [
      doc(1, { name: "安装手册", original_name: "Install-Guide.PDF" }),
      doc(2, { name: "API 参考", original_name: "api-reference.md" }),
      doc(3, { name: "发布说明", original_name: "release-notes.txt" }),
    ];

    const byName = deriveKnowledgeDocumentList(items, {
      ...QUERY,
      keyword: "安装",
    });
    expect(byName.rows.map((row) => row.name)).toEqual(["安装手册"]);

    const byOriginal = deriveKnowledgeDocumentList(items, {
      ...QUERY,
      keyword: "install-guide",
    });
    expect(byOriginal.rows.map((row) => row.name)).toEqual(["安装手册"]);

    const blank = deriveKnowledgeDocumentList(items, {
      ...QUERY,
      keyword: "   ",
    });
    expect(blank.filteredTotal).toBe(3);
  });

  test("filters by lifecycle status", () => {
    const items = [
      doc(1, { status: "ready" }),
      doc(2, { status: "failed" }),
      doc(3, { status: "processing" }),
    ];
    const failed = deriveKnowledgeDocumentList(items, {
      ...QUERY,
      status: "failed",
    });
    expect(failed.rows.map((row) => row.status)).toEqual(["failed"]);
  });

  test("keyword and status combine over the complete list, beyond one backend page", () => {
    // 120 ready + 30 failed rows interleaved: more than a backend page (100).
    const items = Array.from({ length: 150 }, (_, i) =>
      doc(i, {
        status: i % 5 === 4 ? "failed" : "ready",
        name: `报告 ${String(i).padStart(3, "0")}`,
      }),
    );
    const view = deriveKnowledgeDocumentList(items, {
      ...QUERY,
      keyword: "报告",
      status: "failed",
    });
    expect(view.filteredTotal).toBe(30);
    expect(view.pageCount).toBe(2);
    expect(view.rows).toHaveLength(KNOWLEDGE_DOCUMENT_LIST_PAGE_SIZE);
  });

  test.each([
    ["created_desc", ["03", "02", "01"]],
    ["created_asc", ["01", "02", "03"]],
    ["name_asc", ["01", "02", "03"]],
    ["name_desc", ["03", "02", "01"]],
  ] as const)("sorts by %s", (sort, expected) => {
    const items = [
      doc(2, { name: "b-02", created_at: "2026-08-02T00:00:00Z" }),
      doc(3, { name: "c-03", created_at: "2026-08-03T00:00:00Z" }),
      doc(1, { name: "a-01", created_at: "2026-08-01T00:00:00Z" }),
    ];
    const view = deriveKnowledgeDocumentList(items, { ...QUERY, sort });
    expect(view.rows.map((row) => row.name.slice(-2))).toEqual([...expected]);
  });

  test("equal sort keys break ties by id so the order is stable", () => {
    const shared = { created_at: "2026-08-10T00:00:00Z", name: "同名" };
    const items = [doc(3, shared), doc(1, shared), doc(2, shared)];
    const byCreated = deriveKnowledgeDocumentList(items, QUERY);
    const byName = deriveKnowledgeDocumentList(items, {
      ...QUERY,
      sort: "name_asc",
    });
    const ids = (rows: readonly KnowledgeDocumentItem[]) =>
      rows.map((row) => row.id);
    expect(ids(byCreated.rows)).toEqual([...ids(byCreated.rows)].sort());
    expect(ids(byName.rows)).toEqual(ids(byCreated.rows));
  });

  test("pages hold 20 rows and expose the page count", () => {
    const items = Array.from({ length: 45 }, (_, i) => doc(i));
    const first = deriveKnowledgeDocumentList(items, QUERY);
    expect(first.rows).toHaveLength(20);
    expect(first.pageCount).toBe(3);
    expect(first.page).toBe(1);

    const last = deriveKnowledgeDocumentList(items, { ...QUERY, page: 3 });
    expect(last.rows).toHaveLength(5);
    expect(last.page).toBe(3);
  });

  test("a page beyond the end clamps back to the last legal page", () => {
    const items = Array.from({ length: 21 }, (_, i) => doc(i));
    const view = deriveKnowledgeDocumentList(items, { ...QUERY, page: 9 });
    expect(view.page).toBe(2);
    expect(view.rows).toHaveLength(1);
  });

  test("an empty filter result stays on page 1 with one empty page", () => {
    const items = [doc(1)];
    const view = deriveKnowledgeDocumentList(items, {
      ...QUERY,
      keyword: "不存在的关键词",
      page: 5,
    });
    expect(view.filteredTotal).toBe(0);
    expect(view.pageCount).toBe(1);
    expect(view.page).toBe(1);
    expect(view.rows).toEqual([]);
  });
});
