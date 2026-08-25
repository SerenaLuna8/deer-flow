import { beforeEach, describe, expect, rs, test } from "@rstest/core";

import { reloadProjectAgentAuthoringState } from "@/components/projects/assets/agent-authoring-recovery";
import {
  getProjectAgentDefinition,
  listProjectAssets,
  type AgentDefinitionResponse,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

rs.mock("@/core/shared-assets", () => ({
  listProjectAssets: rs.fn(),
  getProjectAgentDefinition: rs.fn(),
  projectAssetKey: (...parts: unknown[]) => parts,
  projectAgentDefinitionKey: (...parts: unknown[]) => parts,
}));

const PROJECT_ID = "00000000-0000-4000-8000-000000000002";
const AGENT_ID = "00000000-0000-4000-8000-000000000003";
const VERSION_ID = "00000000-0000-4000-8000-000000000004";

const item = {
  id: AGENT_ID,
  scope: "project",
  project_id: PROJECT_ID,
  slug: "reviewer",
  display_name: "Reviewer",
  status: "active",
  revision: 2,
  definition_id: VERSION_ID,
  created_by_user_id: "00000000-0000-4000-8000-000000000005",
  created_at: "2026-08-25T00:00:00Z",
  updated_at: "2026-08-25T00:00:00Z",
  capabilities: [],
  binding: null,
} as ProjectAssetItem;
const catalog = {
  system_items: [],
  project_items: [item],
  request_id: "catalog",
} as ProjectAssetList;
const aggregate = {
  item,
  definition: {
    definition_id: VERSION_ID,
    agent_id: AGENT_ID,
    description: "Review changes",
    agents_instructions: "# AGENTS",
    soul: "# SOUL",
    identity: "# IDENTITY",
    user_context: "# USER",
    model_ref: "default",
    model_settings: {},
    tool_groups: ["file:read"],
    skill_refs: [],
    mcp_version_ids: [],
    payload_schema_version: 2,
    payload_checksum: "a".repeat(64),
    updated_by_user_id: "00000000-0000-4000-8000-000000000005",
    updated_at: "2026-08-25T00:00:00Z",
  },
  request_id: "definition",
} as AgentDefinitionResponse;

const mockedListAssets = rs.mocked(listProjectAssets);
const mockedGetDefinition = rs.mocked(getProjectAgentDefinition);

describe("Agent authoring recovery requests", () => {
  beforeEach(() => {
    mockedListAssets.mockReset();
    mockedGetDefinition.mockReset();
    mockedListAssets.mockResolvedValue(catalog);
    mockedGetDefinition.mockResolvedValue(aggregate);
  });

  test("forwards one lifecycle AbortSignal to every recovery read", async () => {
    const controller = new AbortController();

    await reloadProjectAgentAuthoringState({
      projectId: PROJECT_ID,
      assetId: AGENT_ID,
      attemptedRevision: 1,
      includeDependencyCatalogs: true,
      signal: controller.signal,
    });

    expect(mockedListAssets.mock.calls.map((call) => call.slice(0, 2))).toEqual(
      [
        [PROJECT_ID, "agents"],
        [PROJECT_ID, "skills"],
        [PROJECT_ID, "mcp-servers"],
        [PROJECT_ID, "agents"],
      ],
    );
    expect(mockedGetDefinition.mock.calls[0]?.slice(0, 2)).toEqual([
      PROJECT_ID,
      AGENT_ID,
    ]);
    const forwardedSignals = [
      ...mockedListAssets.mock.calls.map((call) => call[2]),
      ...mockedGetDefinition.mock.calls.map((call) => call[2]),
    ];
    expect(
      forwardedSignals.every(
        (signal) => signal instanceof AbortSignal && !signal.aborted,
      ),
    ).toBe(true);

    controller.abort();
    expect(forwardedSignals.every((signal) => signal?.aborted)).toBe(true);
  });
});
