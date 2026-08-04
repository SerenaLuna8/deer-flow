import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, rs, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  useRouter: () => ({ push: rs.fn() }),
}));

import { ProjectCard } from "@/components/projects/project-card";
import { ProjectHeader } from "@/components/projects/project-header";
import { ProjectPageHeader } from "@/components/projects/project-page-header";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Shared research",
  icon: "folder",
  role: "admin",
  capabilities: [...CAPABILITIES],
  is_pinned: true,
  last_entered_at: "2026-07-12T10:30:00+08:00",
  member_count: 3,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 3, reserved: 0, limit: 20 },
    storage_bytes: { used: 1_073_741_824, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 1, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 25, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
};

describe("project presentation contracts", () => {
  test("ability pages use compact title-only headers", () => {
    const html = renderToStaticMarkup(
      createElement(ProjectPageHeader, {
        title: "Agent",
        actions: createElement("button", null, "新建 Agent"),
      }),
    );
    const assetShell = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/assets/project-asset-page-shell.tsx",
      ),
      "utf8",
    );
    const memoryPage = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/settings/memory-settings-page.tsx",
      ),
      "utf8",
    );

    expect(html).toContain(">Agent</h1>");
    expect(html).toContain("新建 Agent");
    expect(html).not.toContain("<p");
    expect(assetShell).not.toContain(
      "eyebrow={`${project.display_name} · 项目资产`}",
    );
    expect(assetShell).not.toContain("description={description}");
    expect(memoryPage).not.toContain(
      "description={t.settings.memory.description}",
    );
  });

  test("keeps system MCP binding actions on the list instead of the detail sheet", () => {
    const listSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/assets/project-asset-page-shell.tsx",
      ),
      "utf8",
    );
    const detailSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/assets/project-asset-detail-sheet.tsx",
      ),
      "utf8",
    );

    expect(listSource).toContain("useSyncCurrentProjectSystemMcpBinding");
    expect(listSource).toContain("systemMcpBindingNeedsUpdate");
    expect(detailSource).not.toContain("SystemBindingDialog");
    expect(detailSource).not.toContain("bindingOpen");
    expect(detailSource).not.toContain('"启用到项目"');
  });

  test("project overview uses a compact identity header", () => {
    const html = renderToStaticMarkup(
      createElement(ProjectHeader, { project }),
    );

    expect(html).toContain("Alpha Project");
    expect(html).toContain("Shared research");
    expect(html).toContain("py-3");
    expect(html).toContain("h-[4.75rem]");
    expect(html).toContain("size-10");
    expect(html).not.toContain("py-6");
    expect(html).not.toContain("size-14");
    expect(html).not.toContain("text-3xl");
  });

  test("workspace hides the redundant project section title", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-workbench.tsx"),
      "utf8",
    );
    expect(source).not.toContain("你的项目");
  });

  test("workspace presents projects as a compact library with one create entry", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-workbench.tsx"),
      "utf8",
    );
    const headerSource = source.slice(
      source.indexOf("<header"),
      source.indexOf("</header>"),
    );
    const toolbarSource = source.slice(
      source.indexOf('data-testid="project-toolbar"'),
      source.indexOf('<section aria-label="项目列表">'),
    );
    const pageIntroSource = source.slice(
      source.indexOf("<WorkspaceBody"),
      source.indexOf('data-testid="project-toolbar"'),
    );
    const emptyStateSource = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-empty-state.tsx"),
      "utf8",
    );

    expect(source).toContain('aria-label="筛选项目"');
    expect(source).not.toContain('data-testid="project-create-card"');
    expect(source.match(/创建项目/gu)).toHaveLength(1);
    expect(emptyStateSource).not.toContain("创建项目");
    expect(headerSource).toContain("工作空间");
    expect(pageIntroSource).not.toContain("工作空间");
    expect(pageIntroSource).not.toContain("创建、搜索和整理你参与的项目。");
    expect(toolbarSource).toContain("创建项目");
    expect(source).toContain('aria-pressed={filter === "all"}');
    expect(source).toContain('aria-pressed={filter === "pinned"}');
    expect(source).toContain("ChevronDownIcon");
    expect(source).not.toContain("FolderKanbanIcon");
    expect(source).not.toContain("多项目工作空间");
  });

  test("workspace recovery stays available in a compact disclosure", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/workspace-recovery-section.tsx",
      ),
      "utf8",
    );

    expect(source).toContain("Collapsible");
    expect(source).toContain('data-testid="workspace-recovery-toggle"');
    expect(source).toContain("data-[state=open]:rotate-180");
  });

  test("project editing keeps the existing icon without exposing a text field", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/projects/edit-project-dialog.tsx"),
      "utf8",
    );
    expect(source).not.toMatch(/\bicon\b/u);
    expect(source).not.toContain("图标");
  });

  test("project creation explains and enforces the slug rule in Chinese", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/create-project-dialog.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("PROJECT_SLUG_HELP");
    expect(source).toContain("projectSlugError");
    expect(source).toContain("aria-describedby={");
    expect(source).toContain('"project-slug-help project-slug-error"');
    expect(source).toContain('id="project-slug-help"');
    expect(source).toContain('id="project-slug-error"');
    expect(source).toContain('role="alert"');
  });

  test("card keeps project identity and actions without metadata summaries", () => {
    const html = renderToStaticMarkup(
      createElement(ProjectCard, {
        project,
        onPin: () => undefined,
        onEdit: () => undefined,
      }),
    );
    expect(html).toContain("Alpha Project");
    expect(html).toContain("Shared research");
    expect(html).toContain("编辑项目");
    expect(html).toContain("进入项目");
    expect(html).toContain("lucide-arrow-right");
    expect(html).toContain("text-selection");
    expect(html).not.toContain("Admin");
    expect(html).not.toContain("active");
    expect(html).not.toContain("3 位成员");
    expect(html).not.toContain("Agent 0");
    expect(html).not.toContain("Skill 0");
    expect(html).not.toContain("MCP 0");
    expect(html).not.toContain("项目配额摘要");
    expect(html).not.toContain("成员 3 / 20");
    expect(html).not.toContain("存储 1 GiB / 5 GiB");
    expect(html).not.toContain("运行 1 / 3");
    expect(html).not.toContain("MCP 25 / 10,000");
    expect(html).not.toContain("private_activity");

    const viewer = { ...project, capabilities: ["project.read" as const] };
    expect(
      renderToStaticMarkup(
        createElement(ProjectCard, {
          project: viewer,
          onPin: () => undefined,
          onEdit: () => undefined,
        }),
      ),
    ).not.toContain("编辑项目");
  });

  test("member actions use the resolved self membership identity", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/members/project-members-page.tsx",
      ),
      "utf8",
    );
    expect(source).toMatch(
      /member\.membership_id\s*!==\s*selfMembership\?\.membership_id/u,
    );
    expect(source).not.toContain("member.user_id !== user?.id");
  });
});
