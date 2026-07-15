import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

describe("project Memory page", () => {
  test("injects the shared Memory view from current project scope", () => {
    const component = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/private-work/project-memory-page.tsx",
      ),
      "utf8",
    );
    const route = readFileSync(
      resolve(process.cwd(), "src/app/projects/[project_slug]/memory/page.tsx"),
      "utf8",
    );
    expect(component).toContain("MemorySettingsView");
    expect(component).toContain("usePrivateWorkAccess");
    expect(component).not.toMatch(/\/api\/memory/u);
    expect(route).toContain("useCurrentProject");
    expect(route).not.toMatch(/useProjects|useProjectBySlug|useEnterProject/u);
  });

  test("Viewer controls omit every Memory mutation", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/private-work/project-memory-page.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("projectMemoryPermissions");
    expect(source).toContain("canModify");
    expect(source).toContain("canReload");
    expect(source).toContain("canImport");
    expect(source).toContain("canDelete");
  });
});
