import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  connectProjectConnection,
  configureProjectChannelInstance,
  deleteProjectChannelInstance,
  disconnectProjectConnection,
  listProjectChannelInstances,
  listProjectConnectionProviders,
  listProjectConnections,
  projectChannelInstancesQueryKey,
  projectConnectionsQueryKey,
  setProjectChannelInstanceEnabled,
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
  test("loads strict project channel instances under the account and project cache root", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        instances: [
          {
            id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            provider: "feishu",
            display_name: "Feishu",
            status: "running",
            enabled: true,
            configured: true,
            credential_configured: true,
            public_config: {
              app_id: "cli_public",
              domain: "https://open.feishu.cn",
            },
            updated_at: "2026-08-03T08:00:00Z",
            last_error: null,
          },
          {
            id: null,
            provider: "slack",
            display_name: "Slack",
            status: "unconfigured",
            enabled: false,
            configured: false,
            credential_configured: false,
            public_config: {},
            updated_at: null,
            last_error: null,
          },
        ],
      }),
    );

    const result = await listProjectChannelInstances(access);

    expect(result.instances[0]).toMatchObject({
      provider: "feishu",
      credential_configured: true,
      public_config: { app_id: "cli_public" },
    });
    expect(result.instances[1]).toMatchObject({
      provider: "slack",
      status: "unconfigured",
      id: null,
    });
    expect(projectChannelInstancesQueryKey(scope)).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
      "channel-instances",
    ]);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/api/projects/${scope.projectId}/channel-instances`,
      { signal: undefined },
    );
    expect(JSON.stringify(result)).not.toMatch(/app_secret|credential_value/i);
  });

  test("rejects secret-shaped fields from the cacheable instance read model", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        instances: [
          {
            id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            provider: "feishu",
            display_name: "Feishu",
            status: "running",
            enabled: true,
            configured: true,
            credential_configured: true,
            public_config: {
              app_id: "cli_public",
              app_secret: SECRET,
            },
            updated_at: "2026-08-03T08:00:00Z",
            last_error: null,
          },
        ],
      }),
    );

    await expect(listProjectChannelInstances(access)).rejects.toThrow();
  });

  test("configures a channel imperatively without retaining its secret in TanStack caches", async () => {
    const queryClient = new QueryClient();
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        provider: "feishu",
        display_name: "Feishu",
        status: "starting",
        enabled: true,
        configured: true,
        credential_configured: true,
        public_config: { app_id: "cli_public", domain: "feishu" },
        updated_at: "2026-08-03T08:00:00Z",
        last_error: null,
      }),
    );

    await configureProjectChannelInstance(access, "feishu", {
      publicConfig: { app_id: "cli_public", domain: "feishu" },
      credentials: { app_secret: SECRET },
      enabled: true,
    });

    const init = mockedFetch.mock.calls[0]?.[1];
    expect(mockedFetch.mock.calls[0]?.[0]).toBe(
      `/api/projects/${scope.projectId}/channel-instances/feishu`,
    );
    expect(init).toMatchObject({ method: "PUT" });
    if (typeof init?.body !== "string") {
      throw new Error("expected JSON request body");
    }
    expect(JSON.parse(init.body)).toEqual({
      public_config: { app_id: "cli_public", domain: "feishu" },
      credentials: { app_secret: SECRET },
      enabled: true,
    });
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(queryClient.getQueryCache().getAll())).not.toContain(
      SECRET,
    );
  });

  test("updates and deletes the exact provider instance", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse({
          id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          provider: "feishu",
          display_name: "Feishu",
          status: "disabled",
          enabled: false,
          configured: true,
          credential_configured: true,
          public_config: { app_id: "cli_public" },
          updated_at: "2026-08-03T08:30:00Z",
          last_error: null,
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await setProjectChannelInstanceEnabled(access, "feishu", false);
    await deleteProjectChannelInstance(access, "feishu");

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/api/projects/${scope.projectId}/channel-instances/feishu/disable`,
      `/api/projects/${scope.projectId}/channel-instances/feishu`,
    ]);
    expect(mockedFetch.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
    });
    expect(mockedFetch.mock.calls[1]?.[1]).toMatchObject({
      method: "DELETE",
    });
  });

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
