import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

describe("workspace information priority", () => {
  test("renders the active project task before low-frequency invitation and recovery governance", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-workbench.tsx"),
      "utf8",
    );

    const projectGrid = source.indexOf('data-testid="project-grid"');
    const invitations = source.indexOf("<WorkspaceInvitations");
    const recovery = source.indexOf("<WorkspaceRecoverySection");

    expect(source).toContain("你的项目");
    expect(projectGrid).toBeGreaterThan(-1);
    expect(projectGrid).toBeLessThan(invitations);
    expect(projectGrid).toBeLessThan(recovery);
  });
});
