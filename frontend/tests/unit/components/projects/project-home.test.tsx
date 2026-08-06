import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectHome } from "@/components/projects/project-home";
import type { Project } from "@/core/projects/types";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha Project",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: ["project.read", "shared_assets.read"],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 1 },
    concurrent_runs: { used: 0, reserved: 0, limit: 1 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 1 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "test-request",
};

describe("ProjectHome", () => {
  test("does not render a redundant workspace-return row above overview content", () => {
    const html = renderToStaticMarkup(
      <ProjectHome
        project={project}
        tokenUsageSection={<div data-testid="token-usage">Usage</div>}
      />,
    );

    expect(html).toContain('data-testid="token-usage"');
    expect(html).not.toContain('href="/workspace"');
  });
});
