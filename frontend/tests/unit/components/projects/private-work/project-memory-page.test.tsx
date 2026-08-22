import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectMemoryAccessBoundary } from "@/components/projects/private-work/project-memory-page";
import { I18nProvider } from "@/core/i18n/context";
import type { Project } from "@/core/projects/types";

const projectWithoutMemoryRead: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Memory access boundary",
  icon: "folder",
  role: "viewer",
  capabilities: ["project.read", "project.enter"],
  is_pinned: false,
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
  request_id: "request-alpha",
};

describe("Project Memory access boundary", () => {
  test("renders an explicit 403 without mounting authorized query hooks", () => {
    // No QueryClient or private-work provider is present. Rendering succeeds
    // only when the denied branch stops before AuthorizedProjectMemoryPage.
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <ProjectMemoryAccessBoundary project={projectWithoutMemoryRead} />
      </I18nProvider>,
    );

    expect(html).toContain('role="alert"');
    expect(html).toContain('data-error-status="403"');
    expect(html).toContain("没有访问权限");
    expect(html).toContain("无权访问记忆");
    expect(html).not.toContain("还没有历史版本");
  });
});
