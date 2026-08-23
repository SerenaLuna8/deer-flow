import { describe, expect, test } from "@rstest/core";

import {
  projectMcpStatusToggleState,
  projectSkillStatusToggleState,
} from "@/components/projects/assets/project-asset-view-model";
import type { ProjectAssetItem } from "@/core/shared-assets";

describe("Project Skill catalog status toggle", () => {
  test("allows an Editor to use the remaining Skill status entry", () => {
    const item = {
      capabilities: ["shared_assets.edit"],
      current_version_id: "version-2",
      scope: "project",
      status: "active",
    } as ProjectAssetItem;

    expect(projectSkillStatusToggleState(item)).toMatchObject({
      checked: true,
      disabled: false,
    });
    expect(projectMcpStatusToggleState(item).disabled).toBe(true);
  });
});
