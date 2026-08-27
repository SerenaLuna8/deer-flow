import { describe, expect, test } from "@rstest/core";

import { projectAssetSourceAfterCatalogChange } from "@/components/projects/assets/project-asset-page-shell";
import type { ProjectAssetItem, ProjectAssetList } from "@/core/shared-assets";

const SYSTEM_SKILL = {} as ProjectAssetItem;
const PROJECT_SKILL = {} as ProjectAssetItem;

function catalog(projectItems: ProjectAssetItem[]): ProjectAssetList {
  return {
    request_id: "request-1",
    system_items: [SYSTEM_SKILL],
    project_items: projectItems,
  };
}

describe("Project Skill source tab", () => {
  test("uses the catalog default only once and preserves the Project tab after deleting its last Skill", () => {
    const initialSource = projectAssetSourceAfterCatalogChange({
      currentSource: "project",
      data: catalog([PROJECT_SKILL]),
      kind: "skills",
      initialized: false,
      touched: false,
    });

    expect(initialSource).toBe("project");
    expect(
      projectAssetSourceAfterCatalogChange({
        currentSource: initialSource,
        data: catalog([]),
        kind: "skills",
        initialized: true,
        touched: false,
      }),
    ).toBe("project");
  });

  test("still defaults a first visit with no Project Skills to the System tab", () => {
    expect(
      projectAssetSourceAfterCatalogChange({
        currentSource: "project",
        data: catalog([]),
        kind: "skills",
        initialized: false,
        touched: false,
      }),
    ).toBe("system");
  });
});
