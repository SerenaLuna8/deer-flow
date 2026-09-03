import { beforeEach, describe, expect, rs, test } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));

import { fetch as mockedFetch } from "@/core/api/fetcher";
import { KnowledgeApiError, listKnowledgeBases } from "@/core/knowledge/api";

const fetchMock = mockedFetch as unknown as ReturnType<
  typeof rs.fn<(input: string) => Promise<Response>>
>;

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";

function baseItem(index: number) {
  const suffix = String(index).padStart(12, "0");
  return {
    id: `40000000-0000-4000-8000-${suffix}`,
    project_id: PROJECT_ID,
    name: `库 ${index}`,
    description: "",
    embedding_model_id: "30000000-0000-4000-8000-000000000001",
    reranker_model_id: null,
    retrieval_mode: "semantic",
    summary_index_enabled: false,
    status: "active",
    document_count: 0,
    default_top_k: 4,
    default_score_threshold: 0.2,
    default_relative_cutoff: null,
    delete_error: null,
    created_at: "2026-08-30T00:00:00Z",
    updated_at: "2026-08-30T00:00:00Z",
  };
}

function pageResponse(items: unknown[], total: number, page: number): Response {
  return new Response(
    JSON.stringify({
      items,
      total,
      page,
      page_size: 100,
      request_id: `req-${page}`,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

/** Queues one fetch response per expected page request, in order. */
function queuePages(pages: Array<{ items: unknown[]; total: number }>) {
  let call = 0;
  fetchMock.mockImplementation((input: string) => {
    const index = call;
    call += 1;
    const spec = pages[index];
    if (!spec) {
      throw new Error(`unexpected page request #${index + 1}: ${input}`);
    }
    return Promise.resolve(pageResponse(spec.items, spec.total, index + 1));
  });
}

describe("knowledge list completeness", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  test("stitches every backend page into one complete list", async () => {
    const first = Array.from({ length: 100 }, (_, i) => baseItem(i));
    const second = Array.from({ length: 40 }, (_, i) => baseItem(100 + i));
    queuePages([
      { items: first, total: 140 },
      { items: second, total: 140 },
    ]);

    const response = await listKnowledgeBases(PROJECT_ID);

    expect(response.items).toHaveLength(140);
    expect(response.total).toBe(140);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  test("a premature empty page is an explicit incomplete error, not a partial success", async () => {
    queuePages([
      { items: Array.from({ length: 100 }, (_, i) => baseItem(i)), total: 300 },
      { items: [], total: 300 },
    ]);

    const failure = await listKnowledgeBases(PROJECT_ID).then(
      () => null,
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(KnowledgeApiError);
    expect((failure as KnowledgeApiError).code).toBe("INCOMPLETE_LIST");
  });

  test("hitting the page cap without reaching total is an incomplete error", async () => {
    queuePages(
      Array.from({ length: 20 }, (_, page) => ({
        items: Array.from({ length: 100 }, (_, i) => baseItem(page * 100 + i)),
        total: 99_999,
      })),
    );

    const failure = await listKnowledgeBases(PROJECT_ID).then(
      () => null,
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(KnowledgeApiError);
    expect((failure as KnowledgeApiError).code).toBe("INCOMPLETE_LIST");
    expect(fetchMock).toHaveBeenCalledTimes(20);
  });

  test("rejects a changed total rather than publishing a list that skips a surviving row", async () => {
    // Deleting row 50 after page 1 shifts surviving row 101 before page 2's
    // offset. The count still matches, but row 101 is missing from the read.
    queuePages([
      {
        items: Array.from({ length: 100 }, (_, i) => baseItem(i + 1)),
        total: 150,
      },
      {
        items: Array.from({ length: 49 }, (_, i) => baseItem(102 + i)),
        total: 149,
      },
    ]);

    await expect(listKnowledgeBases(PROJECT_ID)).rejects.toMatchObject({
      code: "INCOMPLETE_LIST",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  test("rejects repeated identities even when the reported total stays unchanged", async () => {
    queuePages([
      {
        items: Array.from({ length: 100 }, (_, i) => baseItem(i + 1)),
        total: 150,
      },
      // A replacement/reorder between offset pages repeats row 100, making
      // 150 returned rows contain only 149 distinct resources.
      {
        items: Array.from({ length: 50 }, (_, i) => baseItem(100 + i)),
        total: 150,
      },
    ]);

    const failure = await listKnowledgeBases(PROJECT_ID).then(
      () => null,
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(KnowledgeApiError);
    expect((failure as KnowledgeApiError).code).toBe("INCOMPLETE_LIST");
  });
});
