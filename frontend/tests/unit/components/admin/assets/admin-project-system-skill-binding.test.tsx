import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { adminProjectSystemSkillItems } from "@/components/admin/assets/admin-project-asset-page";
import {
  adminSystemSkillVersionIsBindable,
  isAdminSystemBindingConflict,
} from "@/components/admin/assets/admin-project-system-binding-dialog";
import { SystemAssetSection } from "@/components/projects/assets/system-asset-section";
import { I18nProvider } from "@/core/i18n/context";
import {
  SharedAssetApiError,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

const ASSET_ID = "00000000-0000-4000-8000-000000000010";
const VERSION_ID = "00000000-0000-4000-8000-000000000011";
const TIMESTAMP = "2026-08-13T00:00:00Z";

const item: ProjectAssetItem = {
  id: ASSET_ID,
  scope: "system",
  project_id: null,
  slug: "system-skill",
  display_name: "System Skill",
  description: "System description",
  status: "active",
  current_version_id: VERSION_ID,
  revision: 1,
  capabilities: ["shared_assets.read", "shared_assets.manage_bindings"],
  binding: null,
  created_by_user_id: "system",
  created_at: TIMESTAMP,
  updated_at: TIMESTAMP,
};

const data: ProjectAssetList = {
  request_id: "request-1",
  system_items: [item],
  project_items: [],
};

describe("admin project System Skill binding", () => {
  test("selects and renders System Skills only on the Skill project page", () => {
    expect(adminProjectSystemSkillItems(data, "skills")).toEqual([item]);
    expect(adminProjectSystemSkillItems(data, "agents")).toEqual([]);

    const markup = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <SystemAssetSection
          kind="skills"
          items={adminProjectSystemSkillItems(data, "skills")}
          onManageBinding={() => undefined}
        />
      </I18nProvider>,
    );
    expect(markup).toContain("System Skill");
    expect(markup).toContain("管理绑定");
  });

  test("keeps revoked versions visible but not bindable", () => {
    expect(
      adminSystemSkillVersionIsBindable({
        relation: "current",
        governance_status: "active",
        binding_eligible: true,
      }),
    ).toBe(true);
    expect(
      adminSystemSkillVersionIsBindable({
        relation: "current",
        governance_status: "revoked",
        binding_eligible: false,
      }),
    ).toBe(false);
  });

  test("recognizes stale admin binding revisions", () => {
    expect(
      isAdminSystemBindingConflict(
        new SharedAssetApiError(409, "ASSET_CONFLICT", "changed"),
      ),
    ).toBe(true);
  });
});
