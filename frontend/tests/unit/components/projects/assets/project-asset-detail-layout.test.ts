import { describe, expect, test } from "@rstest/core";

import { projectAssetDetailVersionPickerPlacement } from "@/components/projects/assets/project-asset-detail-sheet";

describe("Project asset detail version picker placement", () => {
  test("places the Agent picker before its editor while preserving the Skill history layout", () => {
    expect(projectAssetDetailVersionPickerPlacement("agents")).toBe(
      "before-editor",
    );
    expect(projectAssetDetailVersionPickerPlacement("skills")).toBe(
      "version-section",
    );
  });
});
