import { describe, expect, it } from "@rstest/core";

import {
  isKnowledgeConflictError,
  KnowledgeApiError,
} from "@/core/knowledge/api";

describe("isKnowledgeConflictError", () => {
  it("matches the backend KNOWLEDGE_CONFLICT envelope", () => {
    const error = new KnowledgeApiError(409, "REQUEST_FAILED", "conflict", {
      knowledgeCode: "KNOWLEDGE_CONFLICT",
      serverMessage: "文档内容已更新，请刷新后重试",
    });
    expect(isKnowledgeConflictError(error)).toBe(true);
  });

  it("ignores other knowledge error codes", () => {
    const nameConflict = new KnowledgeApiError(
      409,
      "REQUEST_FAILED",
      "name conflict",
      { knowledgeCode: "KNOWLEDGE_NAME_CONFLICT" },
    );
    expect(isKnowledgeConflictError(nameConflict)).toBe(false);
    const network = new KnowledgeApiError(0, "NETWORK_ERROR", "offline");
    expect(isKnowledgeConflictError(network)).toBe(false);
  });

  it("ignores foreign error shapes", () => {
    expect(isKnowledgeConflictError(new Error("KNOWLEDGE_CONFLICT"))).toBe(
      false,
    );
    expect(isKnowledgeConflictError(undefined)).toBe(false);
    expect(isKnowledgeConflictError("KNOWLEDGE_CONFLICT")).toBe(false);
  });
});
