import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectCard } from "@/components/projects/project-card";
import { ProjectHome } from "@/components/projects/project-home";
import { ProjectPrivateWorkCta } from "@/components/projects/project-private-work-cta";
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
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
};

function renderWithQueryClient(node: ReactNode): string {
  return renderToStaticMarkup(
    createElement(QueryClientProvider, { client: new QueryClient() }, node),
  );
}

describe("project presentation contracts", () => {
  test("card shows public project fields and capability-gated edit only", () => {
    const html = renderToStaticMarkup(
      createElement(ProjectCard, {
        project,
        onPin: () => undefined,
        onEdit: () => undefined,
      }),
    );
    expect(html).toContain("Alpha Project");
    expect(html).toContain("Shared research");
    expect(html).toContain("3 位成员");
    expect(html).toContain("Agent 0");
    expect(html).toContain("Skill 0");
    expect(html).toContain("MCP 0");
    expect(html).toContain("编辑项目");
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

  test("home keeps privacy boundary and leaves navigation to the project shell", () => {
    const html = renderWithQueryClient(createElement(ProjectHome, { project }));
    expect(html).toContain("对话和记忆私有");
    expect(html).toContain("Agent、Skill 和 MCP 共享");
    expect(html).not.toContain("返回工作空间");
    expect(html).not.toContain('href="/workspace"');
    expect(html).not.toContain("/workspace/projects");
    expect(html).not.toContain("项目工作台");
    expect(html).toContain("后续里程碑");
    expect(html).not.toContain("项目切换");
  });

  test("private CTA stays readiness-gated while exposing project chat intent", () => {
    const html = renderWithQueryClient(
      createElement(ProjectPrivateWorkCta, { project }),
    );
    expect(html).toContain("开始私有对话");
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/project-private-work-cta.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("PROJECT_PRIVATE_WORKSPACE");
    expect(source).toContain("useProjectPrivateWorkReadiness");
    const navigationSource = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-nav.tsx"),
      "utf8",
    );
    expect(navigationSource).toContain("useProjectPrivateWorkReadiness");
    expect(navigationSource).toContain("projectPrivateWorkEntryEnabled");
  });

  test("project dialogs expose synchronous submit contracts", () => {
    for (const file of [
      "create-project-dialog.tsx",
      "edit-project-dialog.tsx",
    ]) {
      const source = readFileSync(
        resolve(process.cwd(), "src/components/projects", file),
        "utf8",
      );
      expect(source).toMatch(/onSubmit: \(input: [^)]+\) => void;/u);
      expect(source).not.toContain("mutateAsync");
    }
    const workbench = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-workbench.tsx"),
      "utf8",
    );
    expect(workbench).not.toContain("mutateAsync");
    expect(workbench).toContain("create.mutate(input)");
    expect(workbench).toContain("update.mutate(input)");
  });

  test("project context owns retry and stale enter protection", () => {
    const contextSource = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-context.tsx"),
      "utf8",
    );
    const loaderSource = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-home-loader.tsx"),
      "utf8",
    );
    expect(contextSource).toContain("重试");
    expect(contextSource).toContain(
      '<Link href="/workspace">返回工作空间</Link>',
    );
    expect(contextSource).not.toContain("/workspace/projects");
    expect(contextSource).not.toContain("enter.data ?? projectQuery.data");
    expect(loaderSource).toContain("useCurrentProject");
    expect(loaderSource).not.toMatch(/useProjectBySlug|useEnterProject/u);
  });

  test("lifecycle copy does not claim M2 physically deletes projects", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/settings/project-lifecycle-panel.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("恢复窗口结束后将无法自助恢复");
    expect(source).toMatch(/M2\s+不执行物理清除/u);
    expect(source).not.toContain("永久删除");
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
