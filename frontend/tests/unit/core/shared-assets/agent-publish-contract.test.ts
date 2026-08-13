import { afterEach, expect, rs, test } from "@rstest/core";

import {
  createProjectAgent,
  publishProjectAssetVersion,
} from "@/core/shared-assets/api";
import { projectAgentMutationQueryKeys } from "@/core/shared-assets/hooks";

const ACCOUNT_ID = "00000000-0000-4000-8000-000000000000";
const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const ASSET_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";

afterEach(() => {
  rs.unstubAllGlobals();
});

test("narrows Agent mutation invalidation to its catalog and history", () => {
  expect(
    projectAgentMutationQueryKeys(ACCOUNT_ID, PROJECT_ID, ASSET_ID),
  ).toEqual([
    ["account", ACCOUNT_ID, "shared-assets", "project", PROJECT_ID, "agents"],
    [
      "account",
      ACCOUNT_ID,
      "shared-assets",
      "project",
      PROJECT_ID,
      "agents",
      "asset",
      ASSET_ID,
      "versions",
    ],
  ]);
});

test("publishes an Agent draft through the explicit CAS route", async () => {
  const responseBody = {
    data: {
      id: VERSION_ID,
      agent_id: ASSET_ID,
      version_number: 1,
      workflow_status: "published",
      description: "Reviewer",
      agents_instructions: "# AGENTS",
      soul: "# SOUL",
      identity: "# IDENTITY",
      user_context: "# USER",
      payload_schema_version: 2,
      model_ref: "default",
      model_settings: {},
      tool_groups: ["file:read"],
      skill_version_ids: [],
      mcp_version_ids: [],
      supersedes_version_id: null,
      payload_checksum: "a".repeat(64),
      created_by_user_id: "44444444-4444-4444-8444-444444444444",
      created_at: "2026-08-13T09:00:00Z",
    },
    request_id: "agent-publish-contract",
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
    publishProjectAssetVersion(PROJECT_ID, "agents", ASSET_ID, VERSION_ID, {
      expected_asset_version: 2,
    }),
  ).resolves.toEqual(responseBody);

  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(
      `/api/projects/${PROJECT_ID}/agents/${ASSET_ID}/versions/${VERSION_ID}/publish`,
    ),
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ expected_asset_version: 2 }),
    }),
  );
});

test("creates an Agent only from a complete definition", async () => {
  const input = {
    slug: "reviewer",
    display_name: "Reviewer",
    description: "Reviews changes",
    agents_instructions: "# AGENTS",
    soul: "# SOUL",
    identity: "# IDENTITY",
    user_context: "# USER",
    model_ref: "default",
    model_settings: {},
    tool_groups: ["file:read"],
    skill_version_ids: [],
    mcp_version_ids: [],
  };
  const responseBody = {
    item: {
      id: ASSET_ID,
      scope: "project",
      project_id: PROJECT_ID,
      slug: input.slug,
      display_name: input.display_name,
      status: "suspended",
      current_published_version_id: null,
      version: 2,
      created_by_user_id: "44444444-4444-4444-8444-444444444444",
      created_at: "2026-08-13T09:00:00Z",
      updated_at: "2026-08-13T09:00:00Z",
    },
    version: {
      id: VERSION_ID,
      agent_id: ASSET_ID,
      version_number: 1,
      workflow_status: "draft",
      ...input,
      skill_version_ids: [],
      mcp_version_ids: [],
      supersedes_version_id: null,
      payload_schema_version: 2,
      payload_checksum: "a".repeat(64),
      created_by_user_id: "44444444-4444-4444-8444-444444444444",
      created_at: "2026-08-13T09:00:00Z",
    },
    request_id: "agent-create-contract",
  };
  delete (responseBody.version as Record<string, unknown>).slug;
  delete (responseBody.version as Record<string, unknown>).display_name;
  const fetchMock = rs.fn(
    async () =>
      new Response(JSON.stringify(responseBody), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
  );
  rs.stubGlobal("fetch", fetchMock);

  await expect(createProjectAgent(PROJECT_ID, input)).resolves.toEqual(
    responseBody,
  );
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(`/api/projects/${PROJECT_ID}/agents`),
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify(input),
    }),
  );

  const mismatchedResponse = structuredClone(responseBody);
  mismatchedResponse.version.agent_id = "55555555-5555-4555-8555-555555555555";
  rs.stubGlobal(
    "fetch",
    rs.fn(
      async () =>
        new Response(JSON.stringify(mismatchedResponse), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
  await expect(createProjectAgent(PROJECT_ID, input)).rejects.toThrow(
    /response was invalid/i,
  );
});
