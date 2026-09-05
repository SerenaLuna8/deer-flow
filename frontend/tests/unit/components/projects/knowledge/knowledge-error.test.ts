import { expect, test } from "@rstest/core";

import { knowledgeErrorMessage } from "@/components/projects/knowledge/knowledge-error";
import { enUS, zhCN } from "@/core/i18n/locales";
import { KnowledgeApiError } from "@/core/knowledge/api";

test("RAG legacy profile rejection keeps the explicit server reparse message", () => {
  const message = "原解析配置已不可用，请显式重新解析";
  const error = new KnowledgeApiError(422, "REQUEST_FAILED", message, {
    knowledgeCode: "KNOWLEDGE_PARSE_FAILED",
    serverMessage: message,
  });
  for (const copy of [enUS.knowledge, zhCN.knowledge]) {
    expect(knowledgeErrorMessage(error, copy.errors)).toBe(message);
  }
});
