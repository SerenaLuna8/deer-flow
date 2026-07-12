import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

function readSource(path: string) {
  return readFileSync(resolve(process.cwd(), path), "utf8");
}

describe("workspace route frame", () => {
  test("keeps the project workspace outside the legacy shell", () => {
    const layout = readSource("src/app/workspace/layout.tsx");
    const framePath = resolve(
      process.cwd(),
      "src/app/workspace/workspace-route-frame.tsx",
    );
    expect(existsSync(framePath)).toBe(true);
    if (!existsSync(framePath)) return;
    const frame = readFileSync(framePath, "utf8");

    expect(layout).toContain("<WorkspaceRouteFrame");
    expect(frame).toContain('pathname === "/workspace"');
    expect(frame).toContain('pathname === "/workspace/projects"');
    expect(frame).toContain("legacyShell");
    expect(layout).toContain("<WorkspaceContent");
  });

  test("renders the normal workspace directly and redirects its old alias", () => {
    const workspacePage = readSource("src/app/workspace/page.tsx");
    const projectsPage = readSource("src/app/workspace/projects/page.tsx");

    expect(workspacePage).toContain("<ProjectWorkbenchPage />");
    expect(workspacePage).toContain("NEXT_PUBLIC_STATIC_WEBSITE_ONLY");
    expect(projectsPage).toContain('redirect("/workspace")');
    expect(projectsPage).not.toContain("ProjectWorkbenchPage");
  });
});
