import { describe, expect, test } from "@rstest/core";

import { connectionAgentRuntimeOptions } from "@/components/projects/private-work/project-connections-page";
import type { ProjectAssetItem } from "@/core/shared-assets";

const AGENT: ProjectAssetItem = {
  id: "22222222-2222-4222-8222-222222222222",
  scope: "project",
  project_id: "11111111-1111-4111-8111-111111111111",
  slug: "analyst",
  display_name: "Analyst",
  status: "active",
  current_published_version_id: "33333333-3333-4333-8333-333333333333",
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-27T00:00:00Z",
  updated_at: "2026-07-27T00:00:00Z",
  capabilities: ["shared_assets.read", "shared_assets.execute"],
  binding: null,
};

describe("Project Connections Agent runtime gate", () => {
  test("offers only MCP-verified Agents and keeps blocked Agents explicit", () => {
    const blocked = {
      ...AGENT,
      id: "44444444-4444-4444-8444-444444444444",
      display_name: "Legacy MCP Agent",
    };
    const result = connectionAgentRuntimeOptions(
      [AGENT, blocked],
      [
        { status: "ready", reason: null },
        {
          status: "blocked",
          reason: "该 MCP 版本当前不能作为 Agent 依赖",
        },
      ],
    );

    expect(result.readyAgents.map(({ id }) => id)).toEqual([AGENT.id]);
    expect(result.blockedAgents.map(({ id }) => id)).toEqual([blocked.id]);
  });

  test("fails closed while dependency verification is still loading", () => {
    const result = connectionAgentRuntimeOptions(
      [AGENT],
      [{ status: "loading", reason: "正在验证 Agent 的 MCP 依赖，请稍候。" }],
    );
    expect(result.readyAgents).toEqual([]);
    expect(result.blockedAgents).toEqual([]);
  });
});
