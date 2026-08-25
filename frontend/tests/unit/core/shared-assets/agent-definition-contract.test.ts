import { afterEach, expect, rs, test } from "@rstest/core";

import {
  changeAdminProjectAssetStatus,
  changeProjectAssetStatus,
  createProjectAgent,
  enableProjectSystemBinding,
  getAdminAgentDefinition,
  getAdminProjectAgentDefinition,
  getProjectAgentDefinition,
  updateProjectAgentCapabilityBindings,
  updateProjectAgentInstructions,
} from "@/core/shared-assets/api";
import { projectAgentMutationQueryKeys } from "@/core/shared-assets/hooks";

const ACCOUNT_ID = "00000000-0000-4000-8000-000000000000";
const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const ASSET_ID = "22222222-2222-4222-8222-222222222222";
const DEFINITION_ID = "33333333-3333-4333-8333-333333333333";
const USER_ID = "44444444-4444-4444-8444-444444444444";

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
  skill_refs: [],
  mcp_version_ids: [],
};

const aggregate = {
  item: {
    id: ASSET_ID,
    scope: "project",
    project_id: PROJECT_ID,
    slug: input.slug,
    display_name: input.display_name,
    status: "suspended",
    definition_id: DEFINITION_ID,
    revision: 1,
    created_by_user_id: USER_ID,
    created_at: "2026-08-13T09:00:00Z",
    updated_at: "2026-08-13T09:00:00Z",
  },
  definition: {
    definition_id: DEFINITION_ID,
    agent_id: ASSET_ID,
    description: input.description,
    agents_instructions: input.agents_instructions,
    soul: input.soul,
    identity: input.identity,
    user_context: input.user_context,
    payload_schema_version: 2,
    model_ref: input.model_ref,
    model_settings: input.model_settings,
    tool_groups: input.tool_groups,
    skill_refs: input.skill_refs,
    mcp_version_ids: input.mcp_version_ids,
    payload_checksum: "a".repeat(64),
    updated_by_user_id: USER_ID,
    updated_at: "2026-08-13T09:00:00Z",
  },
  request_id: "agent-definition-contract",
} as const;

afterEach(() => {
  rs.unstubAllGlobals();
});

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("keys Project Agent mutations by its catalog and single Definition", () => {
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
      "definition",
    ],
  ]);
});

test("reads the one Project Agent Definition from the aggregate endpoint", async () => {
  const fetchMock = rs.fn(async () => response(aggregate));
  rs.stubGlobal("fetch", fetchMock);

  await expect(
    getProjectAgentDefinition(PROJECT_ID, ASSET_ID),
  ).resolves.toEqual(aggregate);
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(`/api/projects/${PROJECT_ID}/agents/${ASSET_ID}`),
    expect.objectContaining({ signal: undefined }),
  );
});

test("reads admin Agent details from Definition endpoints without version history", async () => {
  const systemAggregate = {
    ...aggregate,
    item: {
      ...aggregate.item,
      scope: "system",
      project_id: null,
    },
  };
  const fetchMock = rs.fn(async (_input: string) => response(systemAggregate));
  rs.stubGlobal("fetch", fetchMock);

  await expect(getAdminAgentDefinition(ASSET_ID)).resolves.toEqual(
    systemAggregate,
  );
  await expect(
    getAdminProjectAgentDefinition(PROJECT_ID, ASSET_ID),
  ).resolves.toEqual(systemAggregate);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    expect.stringContaining(`/api/admin/assets/agents/${ASSET_ID}`),
    expect.stringContaining(
      `/api/admin/projects/${PROJECT_ID}/assets/agents/${ASSET_ID}`,
    ),
  ]);
  expect(
    fetchMock.mock.calls.every(([url]) => !url.includes("/versions")),
  ).toBe(true);
});

test("accepts the Definition identity returned by an Agent system binding", async () => {
  const binding = {
    project_id: PROJECT_ID,
    kind: "agent",
    asset_id: ASSET_ID,
    definition_id: DEFINITION_ID,
    enabled: true,
    version: 1,
    created_by_user_id: USER_ID,
    updated_by_user_id: USER_ID,
    created_at: "2026-08-13T09:00:00Z",
    updated_at: "2026-08-13T09:00:00Z",
    request_id: "agent-binding",
  } as const;
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => response(binding)),
  );

  await expect(
    enableProjectSystemBinding(PROJECT_ID, "agent", { asset_id: ASSET_ID }),
  ).resolves.toEqual(binding);
});

test("creates an Agent with its initial mutable Definition", async () => {
  const fetchMock = rs.fn(async () => response(aggregate, 201));
  rs.stubGlobal("fetch", fetchMock);

  await expect(createProjectAgent(PROJECT_ID, input)).resolves.toEqual(
    aggregate,
  );
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(`/api/projects/${PROJECT_ID}/agents`),
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify(input),
    }),
  );
});

test("updates Agent instructions in place without creating or activating a version", async () => {
  const fetchMock = rs.fn(async () => response(aggregate));
  rs.stubGlobal("fetch", fetchMock);

  await expect(
    updateProjectAgentInstructions(PROJECT_ID, ASSET_ID, {
      agents_instructions: input.agents_instructions,
      soul: input.soul,
      identity: input.identity,
      user_context: input.user_context,
      expected_revision: 1,
    }),
  ).resolves.toEqual(aggregate);
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(
      `/api/projects/${PROJECT_ID}/agents/${ASSET_ID}/instructions`,
    ),
    expect.objectContaining({ method: "PUT" }),
  );
});

test("updates Agent capability bindings in place", async () => {
  const fetchMock = rs.fn(async () => response(aggregate));
  rs.stubGlobal("fetch", fetchMock);

  await expect(
    updateProjectAgentCapabilityBindings(PROJECT_ID, ASSET_ID, {
      skill_refs: [],
      mcp_version_ids: [],
      expected_revision: 1,
    }),
  ).resolves.toEqual(aggregate);
});

test("parses Project and Admin Agent status mutations as Definition aggregates", async () => {
  const statusResponse = {
    item: {
      ...aggregate.item,
      status: "active",
      revision: 2,
    },
    request_id: "agent-status",
  } as const;
  const fetchMock = rs.fn(async (_input: string) => response(statusResponse));
  rs.stubGlobal("fetch", fetchMock);

  await expect(
    changeProjectAssetStatus(PROJECT_ID, "agents", ASSET_ID, "enable", {
      expected_revision: 1,
    }),
  ).resolves.toEqual(statusResponse);
  await expect(
    changeAdminProjectAssetStatus(PROJECT_ID, "agents", ASSET_ID, "suspend", {
      expected_revision: 2,
    }),
  ).resolves.toEqual(statusResponse);

  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    expect.stringContaining(
      `/api/projects/${PROJECT_ID}/agents/${ASSET_ID}/enable`,
    ),
    expect.stringContaining(
      `/api/admin/projects/${PROJECT_ID}/assets/agents/${ASSET_ID}/suspend`,
    ),
  ]);
});

test("rejects Current Version identity on an Agent status mutation", async () => {
  rs.stubGlobal(
    "fetch",
    rs.fn(async () =>
      response({
        item: {
          ...aggregate.item,
          definition_id: undefined,
          current_version_id: DEFINITION_ID,
        },
        request_id: "obsolete-agent-status",
      }),
    ),
  );

  await expect(
    changeProjectAssetStatus(PROJECT_ID, "agents", ASSET_ID, "enable", {
      expected_revision: 1,
    }),
  ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });
});

test("rejects an obsolete Agent Version payload at the strict Definition boundary", async () => {
  const obsolete = structuredClone(aggregate) as Record<string, unknown>;
  (obsolete.definition as Record<string, unknown>).version_number = 1;
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => response(obsolete)),
  );

  await expect(getProjectAgentDefinition(PROJECT_ID, ASSET_ID)).rejects.toThrow(
    /response was invalid/i,
  );
});

test("rejects an Agent Definition without required model settings", async () => {
  const malformed = structuredClone(aggregate) as Record<string, unknown>;
  delete (malformed.definition as Record<string, unknown>).model_settings;
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => response(malformed)),
  );

  await expect(getProjectAgentDefinition(PROJECT_ID, ASSET_ID)).rejects.toThrow(
    /response was invalid/i,
  );
});
