import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { projectAssetVersionDisplayStatus } from "@/components/projects/assets/project-asset-detail-sheet";
import {
  ProjectAssetListView,
  projectSystemSkillBindingCanManage,
  systemBindingToggleState,
} from "@/components/projects/assets/project-asset-page-shell";
import {
  isSystemBindingConflict,
  systemBindingDialogAvailability,
  systemSkillBindingVersionLabel,
  systemSkillVersionIsBindable,
  systemSkillVersionIsRevoked,
} from "@/components/projects/assets/system-binding-dialog";
import {
  SharedAssetApiError,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

const PROJECT_ID = "00000000-0000-4000-8000-000000000001";
const ASSET_ID = "00000000-0000-4000-8000-000000000010";
const VERSION_ID = "00000000-0000-4000-8000-000000000011";
const TIMESTAMP = "2026-08-13T00:00:00Z";

function systemSkill(
  overrides: Partial<ProjectAssetItem> = {},
): ProjectAssetItem {
  return {
    id: ASSET_ID,
    scope: "system",
    project_id: null,
    slug: "system-skill",
    display_name: "System Skill",
    description: "System description",
    status: "active",
    current_published_version_id: VERSION_ID,
    version: 1,
    capabilities: ["shared_assets.read", "shared_assets.manage_bindings"],
    binding: null,
    created_by_user_id: "system",
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
    ...overrides,
  };
}

function catalog(item = systemSkill()): ProjectAssetList {
  return { request_id: "request-1", system_items: [item], project_items: [] };
}

describe("project System Skill binding", () => {
  test("keeps the version-management action out of the scan-first list", () => {
    const enabled = renderToStaticMarkup(
      <ProjectAssetListView
        kind="skills"
        data={catalog()}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );
    const denied = renderToStaticMarkup(
      <ProjectAssetListView
        kind="skills"
        data={catalog()}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(enabled).not.toContain("管理项目版本");
    expect(denied).not.toContain("管理项目版本");
    expect(enabled).toContain('aria-haspopup="dialog"');
    expect(enabled).toContain('aria-label="查看 System Skill 详情"');
    expect(enabled).not.toContain("lucide-arrow-right");
    expect(enabled).not.toContain("项目启用");
    expect(
      systemBindingToggleState(
        systemSkill({ capabilities: ["shared_assets.read"] }),
        "skills",
      ).disabled,
    ).toBe(true);
  });

  test("keeps an enabled binding manageable after its asset is suspended", () => {
    const item = systemSkill({
      status: "suspended",
      binding: {
        project_id: PROJECT_ID,
        kind: "skill",
        asset_id: ASSET_ID,
        version_id: VERSION_ID,
        enabled: true,
        version: 2,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
    });

    expect(projectSystemSkillBindingCanManage(item, true)).toBe(true);
    expect(projectSystemSkillBindingCanManage(item, false)).toBe(false);
  });

  test("shows revoked history but excludes it from submit targets", () => {
    const revoked = {
      workflow_status: "published" as const,
      governance_status: "revoked" as const,
      binding_eligible: false,
    };
    expect(systemSkillVersionIsRevoked(revoked)).toBe(true);
    expect(systemSkillVersionIsBindable(revoked)).toBe(false);
    expect(
      systemSkillBindingVersionLabel({ ...revoked, version_number: 1 }),
    ).toBe("版本 1（已撤销，不可绑定）");
    expect(
      systemBindingDialogAvailability({
        historyLoading: false,
        historyError: false,
        historyRetryPending: false,
        mutationPending: false,
        selectedVersionId: VERSION_ID,
        publishedVersionIds: [],
        boundVersionId: null,
      }).canSubmit,
    ).toBe(false);
    expect(
      projectAssetVersionDisplayStatus({
        workflow_status: "published",
        governance_status: "revoked",
      } as AssetVersion),
    ).toBe("revoked");
  });

  test("recognizes only optimistic binding conflicts for refresh and reselect", () => {
    expect(
      isSystemBindingConflict(
        new SharedAssetApiError(409, "ASSET_CONFLICT", "changed"),
      ),
    ).toBe(true);
    expect(
      isSystemBindingConflict(
        new SharedAssetApiError(422, "ASSET_VALIDATION_FAILED", "invalid"),
      ),
    ).toBe(false);
  });
});
