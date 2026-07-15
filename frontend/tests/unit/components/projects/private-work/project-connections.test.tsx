import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

describe("project connections page", () => {
  test("uses current project scope and imperative connection functions", () => {
    const component = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/private-work/project-connections-page.tsx",
      ),
      "utf8",
    );
    const route = readFileSync(
      resolve(
        process.cwd(),
        "src/app/projects/[project_slug]/connections/page.tsx",
      ),
      "utf8",
    );
    expect(component).toContain("usePrivateWorkAccess");
    expect(component).toContain("connectProjectConnection");
    expect(component).toContain("disconnectProjectConnection");
    expect(component).not.toMatch(
      /useMutation|runtime-config|\/api\/channels/u,
    );
    expect(route).toContain("useCurrentProject");
    expect(route).not.toMatch(/useProjects|useProjectBySlug|useEnterProject/u);
  });

  test("Viewer has no connect, disconnect, or rebind capability", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/private-work/project-connections-page.tsx",
      ),
      "utf8",
    );
    expect(source).toContain('includes("private_work.create")');
    expect(source).toContain("canManage");
  });
});
