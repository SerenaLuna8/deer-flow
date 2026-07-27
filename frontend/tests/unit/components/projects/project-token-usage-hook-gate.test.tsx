import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/components/projects/project-context", () => ({
  useCurrentProject: rs.fn(),
}));
rs.mock("@/components/projects/project-token-usage-section", () => ({
  ProjectTokenUsageSection: rs.fn(() =>
    createElement("section", { "data-testid": "token-usage-query-mounted" }),
  ),
}));

import { useCurrentProject } from "@/components/projects/project-context";
import { ProjectHomeLoader } from "@/components/projects/project-home-loader";
import { ProjectTokenUsageSection } from "@/components/projects/project-token-usage-section";
import type { Project } from "@/core/projects/types";

const project: Project = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: ["project.read"],
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

describe("project overview token query gate", () => {
  beforeEach(() => {
    rs.clearAllMocks();
  });

  test("mounts the query section only with project.usage.read", () => {
    rs.mocked(useCurrentProject).mockReturnValue(project);
    const denied = renderToStaticMarkup(<ProjectHomeLoader />);
    expect(denied).not.toContain("token-usage-query-mounted");
    expect(ProjectTokenUsageSection).not.toHaveBeenCalled();

    rs.mocked(useCurrentProject).mockReturnValue({
      ...project,
      capabilities: ["project.read", "project.usage.read"],
    });
    const allowed = renderToStaticMarkup(<ProjectHomeLoader />);
    expect(allowed).toContain("token-usage-query-mounted");
    expect(ProjectTokenUsageSection).toHaveBeenCalledTimes(1);
  });
});
