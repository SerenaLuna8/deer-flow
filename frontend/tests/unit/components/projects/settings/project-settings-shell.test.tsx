import { describe, expect, test } from "@rstest/core";

import { projectSettingsNavigationItems } from "@/components/projects/settings/project-settings-shell";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const adminProject: Project = {
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
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
};

describe("projectSettingsNavigationItems", () => {
  test("keeps members inside settings and leaves audit outside", () => {
    const items = projectSettingsNavigationItems(adminProject);
    expect(items.map((item) => item.label)).toEqual(["常规设置", "项目成员"]);
    expect(items.map((item) => item.href)).toEqual([
      "/projects/alpha/settings",
      "/projects/alpha/settings/members",
    ]);
  });
});
