import { beforeEach, describe, expect, rs, test } from "@rstest/core";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  createProjectChannelGroupBindingChallenge,
  deleteProjectChannelGroupBinding,
  listProjectChannelGroupBindings,
  projectChannelGroupBindingsQueryKey,
  updateProjectChannelGroupBinding,
} from "@/core/project-channel-group-bindings";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

const mockedFetch = rs.mocked(fetchWithAuth);
const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const access = {
  scope,
  apiBaseURL: `/api/projects/${scope.projectId}/private-work`,
};
const bindingId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const agentAssetId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const binding = {
  id: bindingId,
  provider: "feishu",
  display_name: "产品讨论群",
  status: "active",
  agent_asset_id: agentAssetId,
  agent_scope: "project",
  last_activity_at: "2026-08-03T09:00:00+00:00",
  revision: 3,
  created_at: "2026-08-03T08:00:00+00:00",
  updated_at: "2026-08-03T09:00:00+00:00",
};

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project channel group bindings API", () => {
  test("loads a strict provider-neutral list under the account and project cache root", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse({ bindings: [binding] }));

    const result = await listProjectChannelGroupBindings(access);

    expect(result.bindings).toEqual([binding]);
    expect(projectChannelGroupBindingsQueryKey(scope)).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
      "channel-group-bindings",
    ]);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/api/projects/${scope.projectId}/channel-group-bindings`,
      { signal: undefined },
    );
  });

  test("rejects server-only chat and channel instance identities", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        bindings: [
          {
            ...binding,
            chat_id: "oc_raw_chat_identity",
            channel_instance_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          },
        ],
      }),
    );

    await expect(listProjectChannelGroupBindings(access)).rejects.toThrow();
  });

  test("creates a Feishu challenge with the exact selected Agent", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        provider: "feishu",
        code: "group-code",
        command: "/bind-project group-code",
        expires_at: "2026-08-03T09:10:00+00:00",
        expires_in: 600,
      }),
    );

    const result = await createProjectChannelGroupBindingChallenge(access, {
      provider: "feishu",
      agentAssetId,
      agentScope: "project",
    });

    expect(result.command).toBe("/bind-project group-code");
    expect(mockedFetch.mock.calls[0]?.[0]).toBe(
      `/api/projects/${scope.projectId}/channel-group-bindings/challenge`,
    );
    const init = mockedFetch.mock.calls[0]?.[1];
    expect(init).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (typeof init?.body !== "string") {
      throw new Error("expected JSON challenge request body");
    }
    expect(JSON.parse(init.body)).toEqual({
      provider: "feishu",
      agent_asset_id: agentAssetId,
      agent_scope: "project",
    });
  });

  test("updates and deletes with optimistic revision protection", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse({ ...binding, status: "disabled", revision: 4 }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await updateProjectChannelGroupBinding(access, bindingId, {
      expectedRevision: 3,
      enabled: false,
    });
    await deleteProjectChannelGroupBinding(access, bindingId, 4);

    expect(mockedFetch.mock.calls[0]?.[0]).toBe(
      `/api/projects/${scope.projectId}/channel-group-bindings/${bindingId}`,
    );
    expect(mockedFetch.mock.calls[0]?.[1]).toMatchObject({ method: "PATCH" });
    const updateBody = mockedFetch.mock.calls[0]?.[1]?.body;
    if (typeof updateBody !== "string") {
      throw new Error("expected JSON update request body");
    }
    expect(JSON.parse(updateBody)).toEqual({
      expected_revision: 3,
      enabled: false,
    });
    expect(mockedFetch.mock.calls[1]?.[0]).toBe(
      `/api/projects/${scope.projectId}/channel-group-bindings/${bindingId}?expected_revision=4`,
    );
    expect(mockedFetch.mock.calls[1]?.[1]).toMatchObject({ method: "DELETE" });
  });

  test("rejects a zero revision before sending a mutation", async () => {
    await expect(
      deleteProjectChannelGroupBinding(access, bindingId, 0),
    ).rejects.toThrow();
    expect(mockedFetch).not.toHaveBeenCalled();
  });
});
