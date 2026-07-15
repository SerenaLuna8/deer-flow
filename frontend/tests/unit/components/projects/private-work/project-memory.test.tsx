import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { MemoryFactList } from "@/components/workspace/settings/memory/memory-workbench";
import { enUS } from "@/core/i18n/locales/en-US";

describe("project Memory page", () => {
  test("injects a project source href and prefers sourceThreadId", () => {
    const html = renderToStaticMarkup(
      <MemoryFactList
        facts={[
          {
            id: "fact-1",
            content: "Project provenance",
            category: "project",
            confidence: 0.9,
            createdAt: "2026-07-15T00:00:00Z",
            source: "legacy-source",
            sourceThreadId: "thread/source",
            sourceRunId: "run-source",
          },
        ]}
        t={enUS}
        isDeleting={false}
        sourceThreadHref={(fact) =>
          `/projects/research-lab/chats/${encodeURIComponent(
            fact.sourceThreadId ?? fact.source,
          )}`
        }
      />,
    );

    expect(html).toContain(
      'href="/projects/research-lab/chats/thread%2Fsource"',
    );
    expect(html).not.toContain("/workspace/chats/");
  });

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
