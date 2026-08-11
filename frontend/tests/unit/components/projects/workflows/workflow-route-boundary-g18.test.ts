import { readFileSync } from "node:fs";

import { describe, expect, test } from "@rstest/core";

function source(path: string): string {
  return readFileSync(path, "utf8");
}

describe("G18 Workflow route boundary", () => {
  test.each([
    "src/app/projects/[project_slug]/workflows/page.tsx",
    "src/app/projects/[project_slug]/workflows/[workflow_id]/page.tsx",
  ])(
    "fails Static closed before loading authenticated workflow modules: %s",
    (path) => {
      const route = source(path);
      const staticGate = route.indexOf("if (isStaticWebsiteOnly()) notFound()");
      const capabilityImport = route.indexOf(
        'await import("@/core/projects/server-capability")',
      );
      const clientImport = route.indexOf(
        'await import("@/components/projects/workflows/',
      );

      expect(staticGate).toBeGreaterThan(-1);
      expect(capabilityImport).toBeGreaterThan(staticGate);
      expect(clientImport).toBeGreaterThan(capabilityImport);
      expect(route).not.toMatch(
        /^import .*@\/core\/(?:api|auth|private-work|project-workflows\/(?:api|definition-api|definition-queries))/mu,
      );
      expect(route).not.toContain("process.env");
      expect(route).not.toContain("PROJECT_WORKFLOW");
    },
  );

  test("gates list and detail direct URLs with workflow.read and keeps slug resolution in the project shell", () => {
    const list = source("src/app/projects/[project_slug]/workflows/page.tsx");
    const detail = source(
      "src/app/projects/[project_slug]/workflows/[workflow_id]/page.tsx",
    );

    for (const route of [list, detail]) {
      expect(route).toContain(
        'await requireServerProjectCapability(slug, "workflow.read")',
      );
      expect(route).not.toContain("lookupServerProjectBySlug");
      expect(route).not.toContain("ProjectContextProvider");
    }
    expect(detail).toContain("workflowDefinitionIdSchema.safeParse");
    expect(
      detail.indexOf(
        'await requireServerProjectCapability(slug, "workflow.read")',
      ),
    ).toBeLessThan(detail.indexOf("workflowDefinitionIdSchema.safeParse"));
  });
});
