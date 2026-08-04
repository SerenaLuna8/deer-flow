import { describe, expect, test } from "@rstest/core";

import type { ProjectAssetList, ProjectAssetItem } from "@/core/shared-assets";
import {
  projectRuntimeSlashSkills,
  projectSlashSkills,
} from "@/core/shared-assets/project-skill-catalog";

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

  test("gives Main every effective Skill in the current project", () => {
    const projectSkill = skill(
      "11111111-1111-4111-8111-111111111111",
      "project",
    );
    const systemSkill = skill(
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
    const disabledSystemSkill = skill(
      "55555555-5555-4555-8555-555555555555",
      "system",
      { binding: null },
    );
    const catalog: ProjectAssetList = {
      project_items: [projectSkill],
      system_items: [systemSkill, disabledSystemSkill],
      request_id: "request-main-skills",
    };

    expect(projectRuntimeSlashSkills(catalog, { kind: "main" })).toEqual([
      {
        name: projectSkill.slug,
        description: projectSkill.display_name,
        enabled: true,
      },
      {
        name: systemSkill.slug,
        description: systemSkill.display_name,
        enabled: true,
      },
    ]);
  });

  test("limits an explicit Agent to the Skill versions declared by that Agent version", () => {
    const referencedProjectVersion = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const referencedSystemVersion = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const projectSkill = skill(
      "11111111-1111-4111-8111-111111111111",
      "project",
      { current_published_version_id: referencedProjectVersion },
    );
    const unreferencedProjectSkill = skill(
      "22222222-2222-4222-8222-222222222222",
      "project",
    );
    const systemSkill = skill(
      "33333333-3333-4333-8333-333333333333",
      "system",
      {
        current_published_version_id: referencedSystemVersion,
        binding: {
          project_id: PROJECT_ID,
          kind: "skill",
          asset_id: "33333333-3333-4333-8333-333333333333",
          version_id: referencedSystemVersion,
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
      project_items: [projectSkill, unreferencedProjectSkill],
      system_items: [systemSkill],
      request_id: "request-explicit-agent-skills",
    };

    expect(
      projectRuntimeSlashSkills(catalog, {
        kind: "explicit",
        skillVersionIds: [referencedSystemVersion],
      }),
    ).toEqual([
      {
        name: systemSkill.slug,
        description: systemSkill.display_name,
        enabled: true,
      },
    ]);
    expect(projectSlashSkills(catalog).map((item) => item.name)).toEqual([
      projectSkill.slug,
      unreferencedProjectSkill.slug,
      systemSkill.slug,
    ]);
  });

  test("resolves an explicitly referenced historical project Skill version", () => {
    const referencedVersion = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const currentVersion = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const projectSkill = skill(
      "11111111-1111-4111-8111-111111111111",
      "project",
      { current_published_version_id: currentVersion },
    );
    const catalog: ProjectAssetList = {
      project_items: [projectSkill],
      system_items: [],
      request_id: "request-historical-project-skill",
    };

    expect(
      projectRuntimeSlashSkills(catalog, {
        kind: "explicit",
        skillVersionIds: [referencedVersion],
        publishedProjectVersionIdsByAssetId: new Map([
          [projectSkill.id, new Set([referencedVersion, currentVersion])],
        ]),
      }),
    ).toEqual([
      {
        name: projectSkill.slug,
        description: projectSkill.display_name,
        enabled: true,
      },
    ]);
  });
});
