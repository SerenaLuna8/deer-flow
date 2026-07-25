import { describe, expect, test } from "@rstest/core";

import type { ProjectAssetList, ProjectAssetItem } from "@/core/shared-assets";
import { projectSlashSkills } from "@/core/shared-assets/project-skill-catalog";

const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const VERSION_ID = "44444444-4444-4444-8444-444444444444";

function skill(
  id: string,
  scope: "project" | "system",
  overrides: Partial<ProjectAssetItem> = {},
): ProjectAssetItem {
  return {
    id,
    scope,
    project_id: scope === "project" ? PROJECT_ID : null,
    slug: `skill-${id.slice(0, 4)}`,
    display_name: `Skill ${id.slice(0, 4)}`,
    status: "active",
    current_published_version_id: VERSION_ID,
    version: 1,
    created_by_user_id: "user-1",
    created_at: "2026-07-25T00:00:00Z",
    updated_at: "2026-07-25T00:00:00Z",
    capabilities: ["shared_assets.read", "shared_assets.execute"],
    binding: null,
    ...overrides,
  };
}

describe("project slash Skill catalog", () => {
  test("omits empty assets and enables only executable published choices", () => {
    const projectPublished = skill(
      "11111111-1111-4111-8111-111111111111",
      "project",
    );
    const projectEmpty = skill(
      "22222222-2222-4222-8222-222222222222",
      "project",
      { current_published_version_id: null },
    );
    const systemBound = skill(
      "33333333-3333-4333-8333-333333333333",
      "system",
      {
        binding: {
          project_id: PROJECT_ID,
          kind: "skill",
          asset_id: "33333333-3333-4333-8333-333333333333",
          version_id: VERSION_ID,
          enabled: true,
          version: 1,
          created_by_user_id: "user-1",
          updated_by_user_id: "user-1",
          created_at: "2026-07-25T00:00:00Z",
          updated_at: "2026-07-25T00:00:00Z",
        },
      },
    );
    const systemEmpty = skill(
      "55555555-5555-4555-8555-555555555555",
      "system",
      {
        current_published_version_id: null,
        binding: {
          project_id: PROJECT_ID,
          kind: "skill",
          asset_id: "55555555-5555-4555-8555-555555555555",
          version_id: VERSION_ID,
          enabled: true,
          version: 1,
          created_by_user_id: "user-1",
          updated_by_user_id: "user-1",
          created_at: "2026-07-25T00:00:00Z",
          updated_at: "2026-07-25T00:00:00Z",
        },
      },
    );
    const catalog: ProjectAssetList = {
      project_items: [projectPublished, projectEmpty],
      system_items: [systemBound, systemEmpty],
      request_id: "request-skills",
    };

    expect(projectSlashSkills(catalog)).toEqual([
      {
        name: projectPublished.slug,
        description: projectPublished.display_name,
        enabled: true,
      },
      {
        name: systemBound.slug,
        description: systemBound.display_name,
        enabled: true,
      },
    ]);
  });
});
