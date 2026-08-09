import { describe, expect, test } from "@rstest/core";

import { agentDependencyOptions } from "@/components/projects/assets/agent-capability-workbench";
import {
  projectAgentVersionCanRestore,
  projectAssetDetailShowsVersionHistory,
} from "@/components/projects/assets/project-asset-detail-sheet";
import type { ProjectAssetList } from "@/core/shared-assets";

const PROJECT_ID = "00000000-0000-4000-8000-000000000001";
const SYSTEM_ASSET_ID = "00000000-0000-4000-8000-000000000010";
const SYSTEM_VERSION_ID = "00000000-0000-4000-8000-000000000011";
const PROJECT_ASSET_ID = "00000000-0000-4000-8000-000000000020";
const PROJECT_VERSION_ID = "00000000-0000-4000-8000-000000000021";
const TIMESTAMP = "2026-08-09T00:00:00Z";

function catalog(): ProjectAssetList {
  return {
    request_id: "request-1",
    system_items: [
      {
        id: SYSTEM_ASSET_ID,
        scope: "system",
        project_id: null,
        slug: "system-skill",
        display_name: "System Skill",
        description: "System description",
        status: "active",
        current_published_version_id: SYSTEM_VERSION_ID,
        version: 1,
        capabilities: ["shared_assets.read"],
        binding: {
          project_id: PROJECT_ID,
          kind: "skill",
          asset_id: SYSTEM_ASSET_ID,
          version_id: SYSTEM_VERSION_ID,
          enabled: true,
          version: 1,
          created_by_user_id: "user-1",
          updated_by_user_id: "user-1",
          created_at: TIMESTAMP,
          updated_at: TIMESTAMP,
        },
        created_by_user_id: "system",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
      {
        id: "00000000-0000-4000-8000-000000000012",
        scope: "system",
        project_id: null,
        slug: "disabled-system-skill",
        display_name: "Disabled System Skill",
        status: "active",
        current_published_version_id: "00000000-0000-4000-8000-000000000013",
        version: 1,
        capabilities: ["shared_assets.read"],
        binding: null,
        created_by_user_id: "system",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
    ],
    project_items: [
      {
        id: PROJECT_ASSET_ID,
        scope: "project",
        project_id: PROJECT_ID,
        slug: "project-skill",
        display_name: "Project Skill",
        description: "Project description",
        status: "active",
        current_published_version_id: PROJECT_VERSION_ID,
        version: 1,
        capabilities: ["shared_assets.read", "shared_assets.edit"],
        binding: null,
        created_by_user_id: "user-1",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
      {
        id: "00000000-0000-4000-8000-000000000022",
        scope: "project",
        project_id: PROJECT_ID,
        slug: "draft-project-skill",
        display_name: "Draft Project Skill",
        status: "active",
        current_published_version_id: null,
        version: 1,
        capabilities: ["shared_assets.read", "shared_assets.edit"],
        binding: null,
        created_by_user_id: "user-1",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
    ],
  };
}

describe("Agent capability bindings", () => {
  test("offers only enabled System bindings and active published Project assets", () => {
    expect(agentDependencyOptions("skill", catalog())).toEqual([
      expect.objectContaining({
        assetId: SYSTEM_ASSET_ID,
        versionId: SYSTEM_VERSION_ID,
        scope: "system",
      }),
      expect.objectContaining({
        assetId: PROJECT_ASSET_ID,
        versionId: PROJECT_VERSION_ID,
        scope: "project",
      }),
    ]);
  });

  test("shows Agent version history in the detail sheet", () => {
    expect(projectAssetDetailShowsVersionHistory("agents")).toBe(true);
  });

  test("restores only a historical published Project Agent version", () => {
    const version = {
      id: PROJECT_VERSION_ID,
      workflow_status: "published" as const,
    };

    expect(
      projectAgentVersionCanRestore(
        "agents",
        "project",
        true,
        version,
        SYSTEM_VERSION_ID,
      ),
    ).toBe(true);
    expect(
      projectAgentVersionCanRestore(
        "agents",
        "project",
        true,
        version,
        PROJECT_VERSION_ID,
      ),
    ).toBe(false);
    expect(
      projectAgentVersionCanRestore(
        "agents",
        "system",
        true,
        version,
        SYSTEM_VERSION_ID,
      ),
    ).toBe(false);
  });
});
