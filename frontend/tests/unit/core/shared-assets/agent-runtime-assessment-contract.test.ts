import { afterEach, expect, rs, test } from "@rstest/core";

import { assessProjectAgentRuntime } from "@/core/shared-assets/api";
import { projectAgentRuntimeAssessmentsKey } from "@/core/shared-assets/query-keys";

const ACCOUNT_ID = "00000000-0000-4000-8000-000000000000";
const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const AGENT_A = "22222222-2222-4222-8222-222222222222";
const AGENT_B = "33333333-3333-4333-8333-333333333333";
const VERSION_A = "44444444-4444-4444-8444-444444444444";

afterEach(() => {
  rs.unstubAllGlobals();
});

test("posts one ordered Agent runtime assessment batch", async () => {
  const responseBody = {
    items: [
      {
        agent_asset_id: AGENT_B,
        selected_version_id: null,
        status: "blocked",
        reason_code: "agent_unavailable",
      },
      {
        agent_asset_id: AGENT_A,
        selected_version_id: VERSION_A,
        status: "ready",
        reason_code: null,
      },
    ],
    request_id: "agent-runtime-contract",
  };
  const fetchMock = rs.fn(
    async () =>
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
  rs.stubGlobal("fetch", fetchMock);

  await expect(
    assessProjectAgentRuntime(PROJECT_ID, [AGENT_B, AGENT_A]),
  ).resolves.toEqual(responseBody);
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(
      `/api/projects/${PROJECT_ID}/agents/runtime-assessments`,
    ),
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ agent_ids: [AGENT_B, AGENT_A] }),
    }),
  );
});

test("rejects response authority fields and reordered items", async () => {
  const responseBody = {
    items: [
      {
        agent_asset_id: AGENT_A,
        selected_version_id: VERSION_A,
        status: "ready",
        reason_code: null,
        project_id: PROJECT_ID,
      },
      {
        agent_asset_id: AGENT_B,
        selected_version_id: null,
        status: "blocked",
        reason_code: "agent_unavailable",
      },
    ],
    request_id: "agent-runtime-invalid",
  };
  rs.stubGlobal(
    "fetch",
    rs.fn(
      async () =>
        new Response(JSON.stringify(responseBody), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );

  await expect(
    assessProjectAgentRuntime(PROJECT_ID, [AGENT_B, AGENT_A]),
  ).rejects.toThrow(/response was invalid/i);

  delete (responseBody.items[0] as Record<string, unknown>).project_id;
  rs.stubGlobal(
    "fetch",
    rs.fn(
      async () =>
        new Response(JSON.stringify(responseBody), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
  await expect(
    assessProjectAgentRuntime(PROJECT_ID, [AGENT_B, AGENT_A]),
  ).rejects.toThrow(/response was invalid/i);
});

test("keys runtime assessments under the exact account and project root", () => {
  expect(
    projectAgentRuntimeAssessmentsKey(ACCOUNT_ID, PROJECT_ID, [
      AGENT_B,
      AGENT_A,
    ]),
  ).toEqual([
    "account",
    ACCOUNT_ID,
    "shared-assets",
    "project",
    PROJECT_ID,
    "agents",
    "runtime-assessments",
    AGENT_B,
    AGENT_A,
  ]);
});
