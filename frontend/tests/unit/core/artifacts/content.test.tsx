import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { useArtifactContent } from "@/core/artifacts/hooks";
import { loadArtifactContent } from "@/core/artifacts/loader";
import {
  createPrivateWorkScopeRegistry,
  transitionPrivateWorkScope,
} from "@/core/private-work/scope-registry";
import type { PrivateWorkAccess } from "@/core/private-work/types";

rs.mock("@/components/workspace/messages/context", () => ({
  useThread: () => ({ thread: { messages: [] }, isMock: false }),
}));
rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

const mockedFetch = rs.mocked(fetchWithAuth);
const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("artifact content scope and transport", () => {
  test("registers project artifact queries under the private-work root and removes them on transition", async () => {
    const queryClient = new QueryClient();
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(scope);

    function Consumer() {
      useArtifactContent({
        filepath: "outputs/report.md",
        threadId: "thread-1",
        enabled: false,
        url: `${access.apiBaseURL}/threads/thread-1/files/file-1`,
        privateWork: access,
      } as Parameters<typeof useArtifactContent>[0] & {
        privateWork: typeof access;
      });
      return null;
    }

    renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <Consumer />
      </QueryClientProvider>,
    );

    const projectQuery = queryClient.getQueryCache().getAll()[0];
    expect(projectQuery?.queryKey).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
      "artifact",
      "outputs/report.md",
      "thread-1",
      false,
      `${access.apiBaseURL}/threads/thread-1/files/file-1`,
    ]);
    expect(projectQuery?.options.retry).toBe(false);

    await transitionPrivateWorkScope(registry, queryClient, scope, {
      ...scope,
      projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    });
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  test("preserves the legacy workspace artifact query key", () => {
    const queryClient = new QueryClient();
    const workspaceAccess = { scope: null } as PrivateWorkAccess;

    function Consumer() {
      useArtifactContent({
        filepath: "outputs/report.md",
        threadId: "thread-1",
        enabled: false,
        url: "/api/threads/thread-1/artifacts/report.md",
        privateWork: workspaceAccess,
      });
      return null;
    }

    renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <Consumer />
      </QueryClientProvider>,
    );

    const workspaceQuery = queryClient.getQueryCache().getAll()[0];
    expect(workspaceQuery?.queryKey).toEqual([
      "artifact",
      "outputs/report.md",
      "thread-1",
      false,
      "/api/threads/thread-1/artifacts/report.md",
    ]);
    expect(workspaceQuery?.options.retry).toBeUndefined();
  });

  test.each([404, 503])(
    "uses authenticated fetch and rejects artifact HTTP %s",
    async (status) => {
      mockedFetch.mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: `private ${status} body` }), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      );

      await expect(
        loadArtifactContent({
          filepath: "outputs/report.md",
          threadId: "thread-1",
          url: "/api/projects/project/private-work/files/file-1",
        }),
      ).rejects.toThrow();
      expect(mockedFetch).toHaveBeenCalledWith(
        "/api/projects/project/private-work/files/file-1",
      );
    },
  );
});
