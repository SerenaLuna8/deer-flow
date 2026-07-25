import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  connectProjectConnection,
  disconnectProjectConnection,
  listProjectConnectionProviders,
  listProjectConnections,
  projectConnectionsQueryKey,
} from "@/core/private-work/connections";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

const mockedFetch = rs.mocked(fetchWithAuth);
const SECRET = "task-16-secret-sentinel";
const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const access = {
  scope,
  apiBaseURL: `/api/projects/${scope.projectId}/private-work`,
};
const agentAssetId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project connection adapter", () => {
  test("loads safe provider health from the exact project path", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        enabled: true,
        providers: [
          {
            provider: "slack",
            display_name: "Slack",
            enabled: true,
            configured: true,
            connectable: true,
            unavailable_reason: null,
            auth_mode: "binding_code",
            connection_status: "not_connected",
          },
        ],
      }),
    );

    const providers = await listProjectConnectionProviders(access);

    expect(providers.enabled).toBe(true);
    expect(providers.providers.map((provider) => provider.provider)).toEqual([
      "slack",
    ]);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/api/projects/${scope.projectId}/connections/providers`,
      { signal: undefined },
    );
  });

  test("lists, connects, and disconnects only through exact project paths", async () => {
    mockedFetch
      .mockResolvedValueOnce(jsonResponse({ connections: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          provider: "slack",
          mode: "binding_code",
          url: null,
          code: "bind-code",
          instruction: "Send the binding code.",
          expires_in: 600,
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    expect(projectConnectionsQueryKey(scope)).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
      "connections",
    ]);
    await listProjectConnections(access);
    await connectProjectConnection(access, "slack", {
      agentAssetId,
      agentScope: "project",
    });
    await disconnectProjectConnection(access, "connection/1");

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/api/projects/${scope.projectId}/connections`,
      `/api/projects/${scope.projectId}/connections/slack/connect`,
      `/api/projects/${scope.projectId}/connections/connection%2F1`,
    ]);
  });

  test("imperative connect leaves no secret in query or mutation caches", async () => {
    const queryClient = new QueryClient();
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        provider: "slack",
        mode: "binding_code",
        url: null,
        code: "bind-code",
        instruction: "Send the binding code.",
        expires_in: 600,
      }),
    );

    await connectProjectConnection(access, "slack", {
      agentAssetId,
      agentScope: "project",
    });
    const snapshot = JSON.stringify({
      queries: queryClient.getQueryCache().getAll(),
      mutations: queryClient.getMutationCache().getAll(),
      sentinel: SECRET.slice(0, 0),
    });
    expect(snapshot).not.toContain(SECRET);
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
  });
});
