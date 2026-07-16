import { describe, expect, test, rs } from "@rstest/core";
import { createElement, Fragment } from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/project-automations/readiness", () => ({
  useProjectAutomationReadiness: rs.fn(() => ({})),
}));
rs.mock("@/core/private-work/readiness", () => ({
  projectPrivateWorkEntryEnabled: (
    featureEnabled: boolean,
    allowed: boolean,
    status?: string,
  ) => featureEnabled && allowed && status === "ready",
  useProjectPrivateWorkReadiness: rs.fn(() => ({})),
}));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => true }));
rs.mock("@/core/threads/hooks", () => ({
  useThreads: rs.fn(() => ({ data: [] })),
}));

import { RecentPrivateWork } from "@/components/projects/private-work/recent-private-work";
import { ProjectDesktopNav } from "@/components/projects/project-nav";
import { ProjectPrivateWorkCta } from "@/components/projects/project-private-work-cta";
import { I18nProvider } from "@/core/i18n/context";
import { useProjectPrivateWorkReadiness } from "@/core/private-work/readiness";
import { useProjectAutomationReadiness } from "@/core/project-automations/readiness";
import type { Project } from "@/core/projects/types";
import { useThreads } from "@/core/threads/hooks";

const project: Project = {
  id: "10000000-0000-4000-8000-000000000001",
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.execute",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

describe("static private-work entry rendering", () => {
  test("renders CTA, navigation and recent wrappers without exposing or querying private work", () => {
    const html = renderToStaticMarkup(
      createElement(
        Fragment,
        null,
        createElement(ProjectPrivateWorkCta, { project }),
        <I18nProvider initialLocale="en-US">
          <ProjectDesktopNav project={project} footer={<span>footer</span>} />
        </I18nProvider>,
        createElement(RecentPrivateWork, { project }),
      ),
    );

    expect(html).not.toContain("开始私有对话");
    expect(html).not.toContain("最近私有对话");
    expect(html).not.toContain('href="/projects/alpha/chats"');
    expect(html).not.toContain('href="/projects/alpha/memory"');
    expect(html).not.toContain('href="/projects/alpha/connections"');
    expect(html).not.toContain('href="/projects/alpha/automations"');
    expect(rs.mocked(useProjectPrivateWorkReadiness).mock.calls).toEqual([
      [false],
      [false],
      [false],
    ]);
    expect(rs.mocked(useProjectAutomationReadiness).mock.calls).toEqual([
      [false],
    ]);
    expect(useThreads).toHaveBeenCalledWith(expect.any(Object), undefined, {
      enabled: false,
    });
  });
});
