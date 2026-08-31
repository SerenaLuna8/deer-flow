import { describe, expect, it } from "@rstest/core";

import {
  KNOWLEDGE_PREVIEW_IDLE,
  knowledgePreviewReducer,
  previewParamsEqual,
  type KnowledgePreviewIdentity,
  type KnowledgePreviewParams,
  type KnowledgePreviewState,
} from "@/core/knowledge/preview-identity";
import type { KnowledgeChunkPreviewResponse } from "@/core/knowledge/types";

function params(
  overrides: Partial<KnowledgePreviewParams> = {},
): KnowledgePreviewParams {
  return {
    chunk_size: 1000,
    chunk_overlap: 100,
    chunk_separator: "\\n\\n",
    remove_extra_spaces: false,
    remove_urls_emails: false,
    chunking_mode: "general",
    ...overrides,
  };
}

function identity(
  file: File,
  sequence: number,
  overrides: Partial<Omit<KnowledgePreviewIdentity, "file" | "sequence">> = {},
): KnowledgePreviewIdentity {
  return {
    file,
    params: params(),
    scopeKey: "acct:proj",
    sequence,
    ...overrides,
  };
}

function response(marker: string): KnowledgeChunkPreviewResponse {
  return {
    items: [{ position: 1, content: marker, word_count: 4, child_contents: [] }],
    total: 1,
    request_id: `req-${marker}`,
  };
}

const fileA = new File(["aaa"], "a.txt", { type: "text/plain" });
const fileB = new File(["bbb"], "b.txt", { type: "text/plain" });

function run(
  ...events: Parameters<typeof knowledgePreviewReducer>[1][]
): KnowledgePreviewState {
  return events.reduce(knowledgePreviewReducer, KNOWLEDGE_PREVIEW_IDLE);
}

describe("knowledgePreviewReducer", () => {
  it("a request starts loading and drops the other file's payload", () => {
    const afterA = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "resolved", scopeKey: "acct:proj", sequence: 1, data: response("A") },
    );
    expect(afterA.status).toBe("success");

    const afterSwitch = knowledgePreviewReducer(afterA, {
      type: "requested",
      identity: identity(fileB, 2),
    });
    expect(afterSwitch.status).toBe("loading");
    expect(afterSwitch.data).toBeNull();
    expect(afterSwitch.error).toBeNull();
    expect(afterSwitch.current?.file).toBe(fileB);
  });

  it("the matching response publishes the payload", () => {
    const state = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "resolved", scopeKey: "acct:proj", sequence: 1, data: response("A") },
    );
    expect(state.status).toBe("success");
    expect(state.data?.items[0]?.content).toBe("A");
  });

  it("a latecomer from a replaced request never overwrites the winner", () => {
    // A is slow, B is fast: B publishes, then A's response finally lands.
    const state = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "requested", identity: identity(fileB, 2) },
      { type: "resolved", scopeKey: "acct:proj", sequence: 2, data: response("B") },
      { type: "resolved", scopeKey: "acct:proj", sequence: 1, data: response("A") },
    );
    expect(state.status).toBe("success");
    expect(state.data?.items[0]?.content).toBe("B");
    expect(state.current?.file).toBe(fileB);
  });

  it("resubmitting identical parameters is a new sequence that wins over the old one", () => {
    const first = identity(fileA, 1);
    const again = identity(fileA, 2);
    const midway = run(
      { type: "requested", identity: first },
      { type: "requested", identity: again },
      { type: "resolved", scopeKey: "acct:proj", sequence: 1, data: response("old") },
    );
    expect(midway.status).toBe("loading");
    expect(midway.data).toBeNull();

    const done = knowledgePreviewReducer(midway, {
      type: "resolved",
      scopeKey: "acct:proj",
      sequence: 2,
      data: response("new"),
    });
    expect(done.status).toBe("success");
    expect(done.data?.items[0]?.content).toBe("new");
  });

  it("a matching failure clears the payload so it cannot pass as a valid preview", () => {
    const boom = new Error("boom");
    const state = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "resolved", scopeKey: "acct:proj", sequence: 1, data: response("A") },
      { type: "requested", identity: identity(fileA, 2) },
      { type: "failed", scopeKey: "acct:proj", sequence: 2, error: boom },
    );
    expect(state.status).toBe("error");
    expect(state.data).toBeNull();
    expect(state.error).toBe(boom);
  });

  it("a late failure from an abandoned request is ignored", () => {
    const midway = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "requested", identity: identity(fileB, 2) },
      { type: "failed", scopeKey: "acct:proj", sequence: 1, error: new Error("late") },
    );
    expect(midway.status).toBe("loading");
    expect(midway.error).toBeNull();

    const done = knowledgePreviewReducer(midway, {
      type: "resolved",
      scopeKey: "acct:proj",
      sequence: 2,
      data: response("B"),
    });
    expect(done.status).toBe("success");
  });

  it("removing the previewed file clears the panel and strands its response", () => {
    const cleared = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "file_removed", file: fileA },
    );
    expect(cleared).toEqual(KNOWLEDGE_PREVIEW_IDLE);

    const afterLate = knowledgePreviewReducer(cleared, {
      type: "resolved",
      scopeKey: "acct:proj",
      sequence: 1,
      data: response("ghost"),
    });
    expect(afterLate).toEqual(KNOWLEDGE_PREVIEW_IDLE);
  });

  it("removing an unrelated file changes nothing", () => {
    const state = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "resolved", scopeKey: "acct:proj", sequence: 1, data: response("A") },
    );
    const after = knowledgePreviewReducer(state, {
      type: "file_removed",
      file: fileB,
    });
    expect(after).toBe(state);
  });

  it("a scope change wipes the panel and strands cross-scope responses", () => {
    const cleared = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "scope_changed", scopeKey: "acct:other" },
    );
    expect(cleared).toEqual(KNOWLEDGE_PREVIEW_IDLE);

    const afterLate = knowledgePreviewReducer(cleared, {
      type: "resolved",
      scopeKey: "acct:proj",
      sequence: 1,
      data: response("ghost"),
    });
    expect(afterLate).toEqual(KNOWLEDGE_PREVIEW_IDLE);
  });

  it("the same scope key is not a reset", () => {
    const state = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "resolved", scopeKey: "acct:proj", sequence: 1, data: response("A") },
    );
    const after = knowledgePreviewReducer(state, {
      type: "scope_changed",
      scopeKey: "acct:proj",
    });
    expect(after).toBe(state);
  });

  it("a response from another scope with a colliding sequence is rejected", () => {
    const state = run(
      { type: "requested", identity: identity(fileA, 1) },
      { type: "resolved", scopeKey: "acct:other", sequence: 1, data: response("X") },
    );
    expect(state.status).toBe("loading");
    expect(state.data).toBeNull();
  });
});

describe("previewParamsEqual", () => {
  it("matches identical general-mode snapshots without child fields", () => {
    expect(previewParamsEqual(params(), params())).toBe(true);
  });

  it("any differing scalar breaks equality", () => {
    expect(previewParamsEqual(params(), params({ chunk_size: 800 }))).toBe(false);
    expect(previewParamsEqual(params(), params({ chunk_separator: "。" }))).toBe(false);
    expect(
      previewParamsEqual(params(), params({ remove_extra_spaces: true })),
    ).toBe(false);
    expect(
      previewParamsEqual(params(), params({ chunking_mode: "parent_child" })),
    ).toBe(false);
  });

  it("parent-child snapshots compare their child fields", () => {
    const left = params({
      chunking_mode: "parent_child",
      child_chunk_size: 500,
      child_chunk_separator: "\\n",
    });
    expect(
      previewParamsEqual(
        left,
        params({
          chunking_mode: "parent_child",
          child_chunk_size: 500,
          child_chunk_separator: "\\n",
        }),
      ),
    ).toBe(true);
    expect(
      previewParamsEqual(
        left,
        params({
          chunking_mode: "parent_child",
          child_chunk_size: 300,
          child_chunk_separator: "\\n",
        }),
      ),
    ).toBe(false);
  });

  it("a snapshot with child fields never equals one without them", () => {
    expect(
      previewParamsEqual(params(), params({ child_chunk_size: 500 })),
    ).toBe(false);
  });
});
