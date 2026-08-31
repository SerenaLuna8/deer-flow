import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { KnowledgeApiError } from "@/core/knowledge/api";
import {
  removeKnowledgeDocumentsCacheForAuthorityError,
  useKnowledgeDocuments,
} from "@/core/knowledge/hooks";
import { knowledgeQueryKey } from "@/core/knowledge/query-keys";
import type { KnowledgeDocumentListResponse } from "@/core/knowledge/types";

const SCOPE = {
  accountId: "90000000-0000-4000-8000-000000000001",
  projectId: "10000000-0000-4000-8000-000000000001",
};
const BASE_ID = "40000000-0000-4000-8000-000000000001";

function seededClient() {
  const client = new QueryClient();
  const queryKey = knowledgeQueryKey(SCOPE, "documents", "list", BASE_ID);
  client.setQueryData(queryKey, { items: [{ name: "private.txt" }] });
  return { client, queryKey };
}

type DocumentsQuery = ReturnType<typeof useKnowledgeDocuments>;

function DocumentsHookProbe({
  baseId,
  onRender,
}: {
  baseId: string;
  onRender: (query: DocumentsQuery) => void;
}) {
  onRender(useKnowledgeDocuments(SCOPE, baseId));
  return null;
}

describe("Knowledge Document cache authority", () => {
  test.each([
    [401, "AUTH_REQUIRED", null],
    [403, "REQUEST_FAILED", "KNOWLEDGE_FORBIDDEN"],
    [404, "REQUEST_FAILED", "KNOWLEDGE_NOT_FOUND"],
  ] as const)(
    "removes cached rows after an authority-boundary %s response",
    (status, code, knowledgeCode) => {
      const { client, queryKey } = seededClient();
      const removed = removeKnowledgeDocumentsCacheForAuthorityError(
        client,
        SCOPE,
        BASE_ID,
        new KnowledgeApiError(status, code, "authority changed", {
          knowledgeCode,
        }),
      );

      expect(removed).toBe(true);
      expect(client.getQueryData(queryKey)).toBeUndefined();
      client.clear();
    },
  );

  test("keeps cached rows for a recoverable refresh failure", () => {
    const { client, queryKey } = seededClient();
    const removed = removeKnowledgeDocumentsCacheForAuthorityError(
      client,
      SCOPE,
      BASE_ID,
      new KnowledgeApiError(0, "NETWORK_ERROR", "offline"),
    );

    expect(removed).toBe(false);
    expect(client.getQueryData(queryKey)).toEqual({
      items: [{ name: "private.txt" }],
    });
    client.clear();
  });

  test("conceals cached rows in the first render that carries an authority error", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const queryKey = knowledgeQueryKey(SCOPE, "documents", "list", BASE_ID);
    const cached = {
      items: [{ name: "private.txt", status: "ready", task_progress: null }],
      total: 1,
      page: 1,
      page_size: 100,
      request_id: "cached",
    } as KnowledgeDocumentListResponse;
    const authorityError = new KnowledgeApiError(
      403,
      "REQUEST_FAILED",
      "authority changed",
      { knowledgeCode: "KNOWLEDGE_FORBIDDEN" },
    );
    client.setQueryData(queryKey, cached);
    try {
      await client.fetchQuery({
        queryKey,
        queryFn: async () => {
          throw authorityError;
        },
      });
    } catch (error) {
      expect(error).toBe(authorityError);
    }
    expect(client.getQueryData(queryKey)).toBe(cached);

    const observed: { current?: DocumentsQuery } = {};
    renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client },
        createElement(DocumentsHookProbe, {
          baseId: BASE_ID,
          onRender: (query) => {
            observed.current = query;
          },
        }),
      ),
    );

    expect(observed.current?.error).toBe(authorityError);
    expect(observed.current?.data).toBeUndefined();
    client.clear();
  });
});
