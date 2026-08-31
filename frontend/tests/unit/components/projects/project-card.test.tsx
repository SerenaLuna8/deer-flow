import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectCard } from "@/components/projects/project-card";
import { I18nProvider } from "@/core/i18n/context";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha-project",
  display_name: "Alpha Project",
  description: "Primary project",
  created_at: "2026-01-12T04:05:00Z",
  icon: "folder",
  role: "admin",
  capabilities: [...CAPABILITIES],
  is_pinned: true,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
};

function renderProject(value: Project, pinPending = false): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <ProjectCard
        project={value}
        onPin={rs.fn()}
        onEdit={rs.fn()}
        pinPending={pinPending}
      />
    </I18nProvider>,
  );
}

describe("project card", () => {
  test("displays the persisted creation time independently of the last visit", () => {
    const html = renderProject({
      ...project,
      last_entered_at: "2026-08-31T12:00:00Z",
    });

    expect(html).toContain('dateTime="2026-01-12T04:05:00Z"');
    expect(html).toContain("Created");
    expect(html).not.toContain('dateTime="2026-08-31T12:00:00Z"');
  });

  test("localizes the creation-time label in Chinese", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <ProjectCard project={project} onPin={rs.fn()} onEdit={rs.fn()} />
      </I18nProvider>,
    );

    expect(html).toContain("创建于");
    expect(html).toContain('dateTime="2026-01-12T04:05:00Z"');
  });

  test("renders the project identity, state, and primary actions", () => {
    const html = renderProject(project);

    expect(html).toContain('data-testid="project-card"');
    expect(html).toContain("<h3");
    expect(html).toContain("Alpha Project");
    expect(html).toContain("alpha-project");
    expect(html).toContain("Primary project");
    expect(html).toContain('aria-label="Unpin project"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-label="Edit project"');
    expect(html).toContain('href="/projects/alpha-project"');
    expect(html).toContain("project-folder.webp");
    expect(html).toContain('alt=""');
  });

  test("keeps mutation buttons outside the named project navigation link", () => {
    const html = renderProject(project);
    const links = [...html.matchAll(/<a\b[^>]*>[\s\S]*?<\/a>/g)];

    expect(links).toHaveLength(1);
    expect(links[0]?.[0]).toContain('aria-label="Open project: Alpha Project"');
    expect(links[0]?.[0]).not.toContain("<button");
    expect(html).toContain('aria-label="Edit project"');
    expect(html).toContain('aria-label="Unpin project"');
  });

  test("keeps pinning disabled while its mutation is pending", () => {
    const html = renderProject(project, true);
    const pinButton = /<button\b[^>]*aria-label="Unpin project"[^>]*>/.exec(
      html,
    )?.[0];

    expect(pinButton).toContain('disabled=""');
    expect(pinButton).toContain('aria-pressed="true"');
  });

  test("preserves a custom project icon and the empty description fallback", () => {
    const html = renderProject({ ...project, icon: "🧪", description: "" });

    expect(html).toContain("🧪");
    expect(html).not.toContain("project-folder.webp");
    expect(html).toContain("No project description");
  });

  test("keeps editing hidden without the server-issued capability", () => {
    const html = renderProject({
      ...project,
      role: "viewer",
      capabilities: project.capabilities.filter(
        (capability) => capability !== "project.update",
      ),
    });

    expect(html).not.toContain('aria-label="Edit project"');
    expect(html).toContain('href="/projects/alpha-project"');
  });
});
