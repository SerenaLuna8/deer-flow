import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { createElement } from "react";
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

  test("home has privacy boundary, placeholders, and only a workspace return", () => {
    const html = renderToStaticMarkup(createElement(ProjectHome, { project }));
    expect(html).toContain("对话和记忆私有");
    expect(html).toContain("Agent、Skill 和 MCP 共享");
    expect(html).toContain("返回工作空间");
    expect(html).toContain('href="/workspace"');
    expect(html).not.toContain("/workspace/projects");
    expect(html).not.toContain("项目工作台");
    expect(html).toContain("后续里程碑");
    expect(html).not.toContain("项目切换");
  });

  test("private CTA is hard-disabled and has no Thread dependency", () => {
    const html = renderToStaticMarkup(createElement(ProjectPrivateWorkCta));
    expect(html).toContain("私有工作区将在后续里程碑开放");
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/project-private-work-cta.tsx",
      ),
      "utf8",
    );
    expect(source).not.toMatch(/threads|createThread|router\.push/iu);
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

  test("home error state offers retry without rendering stale enter data", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-home-loader.tsx"),
      "utf8",
    );
    expect(source).toContain("重试");
    expect(source).toContain('<Link href="/workspace">返回工作空间</Link>');
    expect(source).not.toContain("/workspace/projects");
    expect(source).not.toContain("项目工作台");
    expect(source).not.toContain("enter.data ?? projectQuery.data");
  });
});
