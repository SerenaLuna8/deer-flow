import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentAssetDetail } from "@/components/projects/assets/agent-asset-detail";
import {
  ProjectAgentCardGridView,
  projectAgentChatAvailability,
} from "@/components/projects/assets/project-agents-page";
import type { Capability } from "@/core/projects/types";
import type { AssetVersion, ProjectAssetItem } from "@/core/shared-assets";

const version: Extract<AssetVersion, { agent_id: string }> = {
  id: "11111111-1111-4111-8111-111111111111",
  agent_id: "22222222-2222-4222-8222-222222222222",
  version_number: 4,
  workflow_status: "published",
  description: "Research and compare sources",
  soul: "Be careful and cite evidence.",
  model_ref: "deepseek-v4-pro",
  tool_groups: ["web", "files"],
  skill_version_ids: ["33333333-3333-4333-8333-333333333333"],
  mcp_version_ids: ["44444444-4444-4444-8444-444444444444"],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  created_by_user_id: "editor-1",
  created_at: "2026-07-21T00:00:00Z",
};

const chatCapabilities: Capability[] = [
  "private_work.create",
  "shared_assets.execute",
];

const agentItem: ProjectAssetItem = {
  id: version.agent_id,
  scope: "project",
  project_id: "33333333-3333-4333-8333-333333333333",
  slug: "research-agent",
  display_name: "Research Agent",
  description: "研究多个来源并给出可核验的结论。",
  status: "active",
  current_published_version_id: version.id,
  version: 3,
  created_by_user_id: "editor-1",
  created_at: "2026-07-21T00:00:00Z",
  updated_at: "2026-07-21T00:00:00Z",
  capabilities: [
    "shared_assets.read",
    "shared_assets.execute",
    "shared_assets.edit",
  ],
  binding: null,
};

describe("Agent asset detail", () => {
  test("renders project Agents as cards with separate detail and chat actions", () => {
    const html = renderToStaticMarkup(
      <ProjectAgentCardGridView
        items={[
          agentItem,
          {
            ...agentItem,
            id: "55555555-5555-4555-8555-555555555555",
            display_name: "Coding Agent",
            slug: "coding-agent",
            description: null,
          },
        ]}
        projectCapabilities={chatCapabilities}
        selectedAssetId={null}
        creatingChatForAgentId={null}
        onSelect={() => undefined}
        onStartChat={() => undefined}
      />,
    );

    expect(html).toContain('role="list"');
    expect(html.match(/role="listitem"/gu)).toHaveLength(2);
    expect(html).toContain("研究多个来源并给出可核验的结论。");
    expect(html).toContain("暂无简介，可进入详情完善 Agent 设置。");
    expect(html).toContain("查看 Research Agent 详情");
    expect(html).toContain("与 Research Agent 对话");
    expect(html).not.toContain("research-agent");
    expect(html).not.toContain("已有发布版本");
    expect(html).not.toContain("删除");
  });

  test("enables chat only for an executable published Agent and authorized project", () => {
    expect(projectAgentChatAvailability(agentItem, chatCapabilities)).toEqual({
      enabled: true,
      reason: null,
    });
    expect(
      projectAgentChatAvailability(
        { ...agentItem, current_published_version_id: null },
        chatCapabilities,
      ),
    ).toEqual({
      enabled: false,
      reason: "请先完成 Agent 配置并发布",
    });
    expect(
      projectAgentChatAvailability(agentItem, ["shared_assets.execute"]),
    ).toEqual({
      enabled: false,
      reason: "当前账号没有创建 Agent 对话的权限",
    });
    expect(
      projectAgentChatAvailability(
        { ...agentItem, status: "archived" },
        chatCapabilities,
      ),
    ).toEqual({
      enabled: false,
      reason: "该 Agent 当前不可用",
    });
  });

  test("requires an enabled binding before starting a System Agent chat", () => {
    const systemAgent: ProjectAssetItem = {
      ...agentItem,
      scope: "system",
      project_id: null,
      binding: null,
    };
    const enabledBinding = {
      project_id: "33333333-3333-4333-8333-333333333333",
      kind: "agent" as const,
      asset_id: systemAgent.id,
      version_id: version.id,
      enabled: true,
      version: 1,
      created_by_user_id: "editor-1",
      updated_by_user_id: "editor-1",
      created_at: "2026-07-21T00:00:00Z",
      updated_at: "2026-07-21T00:00:00Z",
    };

    expect(
      projectAgentChatAvailability(systemAgent, chatCapabilities),
    ).toEqual({
      enabled: false,
      reason: "请先在详情中启用该 System Agent",
    });
    expect(
      projectAgentChatAvailability(
        {
          ...systemAgent,
          binding: { ...enabledBinding, enabled: false },
        },
        chatCapabilities,
      ),
    ).toEqual({
      enabled: false,
      reason: "请先在详情中启用该 System Agent",
    });
    expect(
      projectAgentChatAvailability(
        { ...systemAgent, binding: enabledBinding },
        chatCapabilities,
      ),
    ).toEqual({
      enabled: true,
      reason: null,
    });
  });

  test("renders an unavailable chat action with an actionable reason", () => {
    const unpublished = {
      ...agentItem,
      current_published_version_id: null,
    };
    const html = renderToStaticMarkup(
      <ProjectAgentCardGridView
        items={[unpublished]}
        projectCapabilities={chatCapabilities}
        selectedAssetId={unpublished.id}
        creatingChatForAgentId={null}
        onSelect={() => undefined}
        onStartChat={() => undefined}
      />,
    );

    expect(html).toContain("与 Research Agent 对话，请先完成 Agent 配置并发布");
    expect(html).toContain('title="请先完成 Agent 配置并发布"');
    expect(html).toContain('disabled=""');
  });

  test("blocks card chat when fail-closed MCP dependency validation fails", () => {
    const reason =
      "该 MCP 版本当前不能作为 Agent 依赖：当前仅支持 SSE 或 HTTP。";
    expect(
      projectAgentChatAvailability(agentItem, chatCapabilities, reason),
    ).toEqual({
      enabled: false,
      reason,
    });
    const html = renderToStaticMarkup(
      <ProjectAgentCardGridView
        items={[agentItem]}
        projectCapabilities={chatCapabilities}
        selectedAssetId={null}
        creatingChatForAgentId={null}
        mcpDependencyReasons={new Map([[agentItem.id, reason]])}
        onSelect={() => undefined}
        onStartChat={() => undefined}
      />,
    );
    expect(html).toContain(reason);
    expect(html).toContain('disabled=""');
  });

  test("shows the actual Agent definition without inventing runtime analytics", () => {
    const html = renderToStaticMarkup(<AgentAssetDetail version={version} />);

    for (const text of [
      "Research and compare sources",
      "Be careful and cite evidence.",
      "deepseek-v4-pro",
      "web",
      "files",
      "Skill 依赖",
      "MCP 依赖",
    ]) {
      expect(html).toContain(text);
    }
    for (const unsupported of ["成功率", "运行次数", "平均耗时"]) {
      expect(html).not.toContain(unsupported);
    }
  });
});
