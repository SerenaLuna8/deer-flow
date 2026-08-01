import { describe, expect, test } from "@rstest/core";

import { prepareMainProjectChatRuntime } from "@/components/projects/private-work/main-project-chat-runtime";
import type {
  AssetVersion,
  ProjectAssetItem,
  ProjectAssetList,
  VersionHistoryResponse,
} from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const MAIN_ID = "22222222-2222-4222-8222-222222222222";
const MAIN_V1 = "33333333-3333-4333-8333-333333333331";
const MAIN_V2 = "33333333-3333-4333-8333-333333333332";
const SKILL_ID = "44444444-4444-4444-8444-444444444444";
const SKILL_V1 = "55555555-5555-4555-8555-555555555551";
const SKILL_V2 = "55555555-5555-4555-8555-555555555552";
const CREATED_AT = "2026-08-01T00:00:00Z";

function agentVersion(
  id: string,
  versionNumber: number,
  skillVersionIds: string[],
): Extract<AssetVersion, { agent_id: string }> {
  return {
    id,
    agent_id: MAIN_ID,
    version_number: versionNumber,
    workflow_status: "published",
    description: `Main v${versionNumber}`,
    agents_instructions: "",
    soul: "",
    identity: "",
    user_context: "",
    payload_schema_version: 2,
    model_ref: "default",
    tool_groups: [],
    skill_version_ids: skillVersionIds,
    mcp_version_ids: [],
    supersedes_version_id: versionNumber === 1 ? null : MAIN_V1,
    payload_checksum: `main-v${versionNumber}`,
    created_by_user_id: "system",
    created_at: CREATED_AT,
  };
}

function skillVersion(
  id: string,
  versionNumber: number,
): Extract<AssetVersion, { skill_id: string }> {
  return {
    id,
    skill_id: SKILL_ID,
    version_number: versionNumber,
    workflow_status: "published",
    description: `Core skill v${versionNumber}`,
    frontmatter: {},
    compatibility: null,
    secret_requirements: [],
    scan_decision: "allow",
    scan_rule_ids: [],
    scan_summary: {},
    file_views: [],
    supersedes_version_id: versionNumber === 1 ? null : SKILL_V1,
    payload_checksum: `skill-v${versionNumber}`,
    created_by_user_id: "system",
    created_at: CREATED_AT,
  };
}

function mainAgent(
  currentVersionId: string,
  boundVersionId: string,
): ProjectAssetItem {
  return {
    id: MAIN_ID,
    scope: "system",
    project_id: null,
    slug: "project-assistant",
    display_name: "Main",
    status: "active",
    current_published_version_id: currentVersionId,
    version: 2,
    created_by_user_id: "system",
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
    capabilities: [
      "shared_assets.read",
      "shared_assets.execute",
      "shared_assets.manage_bindings",
    ],
    binding: {
      project_id: PROJECT_ID,
      kind: "agent",
      asset_id: MAIN_ID,
      version_id: boundVersionId,
      enabled: true,
      version: 7,
      created_by_user_id: "admin",
      updated_by_user_id: "admin",
      created_at: CREATED_AT,
      updated_at: CREATED_AT,
    },
  };
}

const skill: ProjectAssetItem = {
  id: SKILL_ID,
  scope: "system",
  project_id: null,
  slug: "deerflow-core",
  display_name: "DeerFlow Core",
  status: "active",
  current_published_version_id: SKILL_V2,
  version: 2,
  created_by_user_id: "system",
  created_at: CREATED_AT,
  updated_at: CREATED_AT,
  capabilities: ["shared_assets.read", "shared_assets.manage_bindings"],
  binding: {
    project_id: PROJECT_ID,
    kind: "skill",
    asset_id: SKILL_ID,
    version_id: SKILL_V1,
    enabled: true,
    version: 4,
    created_by_user_id: "admin",
    updated_by_user_id: "admin",
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
  },
};

const agentHistory: VersionHistoryResponse = {
  data: [
    agentVersion(MAIN_V2, 2, [SKILL_V2]),
    agentVersion(MAIN_V1, 1, [SKILL_V1]),
  ],
  request_id: "agent-history",
};
const skillHistory: VersionHistoryResponse = {
  data: [skillVersion(SKILL_V2, 2), skillVersion(SKILL_V1, 1)],
  request_id: "skill-history",
};
const emptyCatalog: ProjectAssetList = {
  system_items: [],
  project_items: [],
  request_id: "empty-catalog",
};

describe("Main project chat runtime", () => {
  test("moves dependencies and Main from an old binding to the current published closure", async () => {
    const moves: unknown[] = [];

    const result = await prepareMainProjectChatRuntime({
      projectId: PROJECT_ID,
      agent: mainAgent(MAIN_V2, MAIN_V1),
      listAssets: async (_projectId, kind) =>
        kind === "skills"
          ? {
              system_items: [skill],
              project_items: [],
              request_id: "skill-catalog",
            }
          : emptyCatalog,
      listVersions: async (_projectId, kind) =>
        kind === "agents" ? agentHistory : skillHistory,
      enableBinding: async () => {
        throw new Error("old enabled bindings must move instead of re-enable");
      },
      moveBinding: async (_projectId, kind, assetId, action, input) => {
        moves.push([kind, assetId, action, input]);
      },
    });

    expect(result).toEqual({ bindingsChanged: true });
    expect(moves).toEqual([
      [
        "skill",
        SKILL_ID,
        "upgrade",
        { version_id: SKILL_V2, expected_binding_version: 4 },
      ],
      [
        "agent",
        MAIN_ID,
        "upgrade",
        { version_id: MAIN_V2, expected_binding_version: 7 },
      ],
    ]);
  });

  test("rolls Main back with the binding revision when current published points older", async () => {
    const moves: unknown[] = [];
    const noDependencyHistory: VersionHistoryResponse = {
      data: [agentVersion(MAIN_V2, 2, []), agentVersion(MAIN_V1, 1, [])],
      request_id: "agent-history-rollback",
    };

    await prepareMainProjectChatRuntime({
      projectId: PROJECT_ID,
      agent: mainAgent(MAIN_V1, MAIN_V2),
      listAssets: async () => emptyCatalog,
      listVersions: async () => noDependencyHistory,
      enableBinding: async () => {
        throw new Error("old enabled Main must move instead of re-enable");
      },
      moveBinding: async (_projectId, kind, assetId, action, input) => {
        moves.push([kind, assetId, action, input]);
      },
    });

    expect(moves).toEqual([
      [
        "agent",
        MAIN_ID,
        "rollback",
        { version_id: MAIN_V1, expected_binding_version: 7 },
      ],
    ]);
  });
});
