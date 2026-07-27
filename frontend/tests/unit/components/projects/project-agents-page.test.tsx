import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AGENT_INSTRUCTION_FILES,
  AgentInstructionWorkspace,
  agentInstructionConflictHasLatestServerState,
  agentInstructionDraft,
  agentInstructionDraftIsDirty,
  agentInstructionSaveIsPending,
} from "@/components/projects/assets/agent-instructions-workbench";
import {
  ProjectAgentCardGridView,
  projectAgentCanActivate,
  projectAgentChatAvailability,
} from "@/components/projects/assets/project-agents-page";
import { effectiveAssetVersion } from "@/components/projects/assets/project-asset-detail-sheet";
import type { Capability } from "@/core/projects/types";
import type { AssetVersion, ProjectAssetItem } from "@/core/shared-assets";

const version: Extract<AssetVersion, { agent_id: string }> = {
  id: "11111111-1111-4111-8111-111111111111",
  agent_id: "22222222-2222-4222-8222-222222222222",
  version_number: 4,
  workflow_status: "published",
  description: "Research and compare sources",
  agents_instructions: "# Agent\n\nResearch carefully.",
  soul: "Be careful and cite evidence.",
  identity: "# Identity\n\nYou are a researcher.",
  user_context: "# User\n\nUse Chinese.",
  payload_schema_version: 2,
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

  test("offers an explicit enable action for a suspended project Agent", () => {
    const suspended = {
      ...agentItem,
      status: "suspended" as const,
      capabilities: [
        ...agentItem.capabilities,
        "shared_assets.manage_bindings" as const,
      ],
    };
    const projectCapabilities = [
      ...chatCapabilities,
      "shared_assets.manage_bindings" as const,
    ];
    const html = renderToStaticMarkup(
      <ProjectAgentCardGridView
        items={[suspended]}
        projectCapabilities={projectCapabilities}
        selectedAssetId={null}
        creatingChatForAgentId={null}
        activatingAgentId={null}
        onSelect={() => undefined}
        onStartChat={() => undefined}
        onActivate={() => undefined}
      />,
    );

    expect(html).toContain("启用 Research Agent");
    expect(html).toContain(">启用<");
    expect(html).toContain("已停用");
    expect(html).not.toContain("已暂停");
    expect(html).toContain("该 Agent 当前不可用");
    expect(projectAgentCanActivate(suspended, projectCapabilities)).toBe(true);
    expect(
      projectAgentCanActivate(
        { ...suspended, capabilities: agentItem.capabilities },
        projectCapabilities,
      ),
    ).toBe(false);
    expect(
      projectAgentCanActivate(
        { ...suspended, current_published_version_id: null },
        projectCapabilities,
      ),
    ).toBe(false);
  });

  test("does not offer enable when only the project-level capability is present", () => {
    const suspended = {
      ...agentItem,
      status: "suspended" as const,
    };
    const html = renderToStaticMarkup(
      <ProjectAgentCardGridView
        items={[suspended]}
        projectCapabilities={[
          ...chatCapabilities,
          "shared_assets.manage_bindings",
        ]}
        selectedAssetId={null}
        creatingChatForAgentId={null}
        activatingAgentId={null}
        onSelect={() => undefined}
        onStartChat={() => undefined}
        onActivate={() => undefined}
      />,
    );

    expect(html).not.toContain("启用 Research Agent");
  });

  test("maps the four fixed documents and renders only the selected file content", () => {
    const draft = agentInstructionDraft(version);
    const html = renderToStaticMarkup(
      <AgentInstructionWorkspace
        draft={draft}
        selectedField="identity"
        displayMode="source"
        editing={false}
        pending={false}
        dirty={false}
        errorMessage={null}
        onSelect={() => undefined}
        onDisplayModeChange={() => undefined}
        onChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
      />,
    );

    expect(AGENT_INSTRUCTION_FILES.map((file) => file.name)).toEqual([
      "AGENTS.md",
      "SOUL.md",
      "IDENTITY.md",
      "USER.md",
    ]);
    for (const filename of AGENT_INSTRUCTION_FILES.map((file) => file.name)) {
      expect(html).toContain(filename);
    }
    expect(html).toContain("You are a researcher.");
    expect(html).not.toContain("Research carefully.");
    expect(html).not.toContain("Be careful and cite evidence.");
    expect(html).not.toContain("Use Chinese.");
    expect(html).toContain("源码");
    expect(html).toContain("预览");
    expect(html).not.toContain("运行配置历史");
    expect(html).not.toContain("运行配置版本");
    for (const unsupported of [
      "新建文件",
      "新建文件夹",
      "删除文件",
      "重命名",
      "面包屑",
    ]) {
      expect(html).not.toContain(unsupported);
    }
  });

  test("detects local edits across any of the four documents", () => {
    const baseline = agentInstructionDraft(version);
    expect(agentInstructionDraftIsDirty(baseline, baseline)).toBe(false);
    expect(
      agentInstructionDraftIsDirty(baseline, {
        ...baseline,
        user_context: `${baseline.user_context}\nMore context`,
      }),
    ).toBe(true);
  });

  test("starts a newly created Agent with four empty logical documents", () => {
    expect(agentInstructionDraft(null)).toEqual({
      agents_instructions: "",
      soul: "",
      identity: "",
      user_context: "",
    });
  });

  test("accepts a newer remote save that wins before the local save refetch", () => {
    const pending = {
      assetId: "22222222-2222-4222-8222-222222222222",
      versionId: "55555555-5555-4555-8555-555555555555",
      assetVersion: 2,
    };

    expect(
      agentInstructionSaveIsPending(
        pending,
        { id: pending.assetId, version: 1 },
        version,
      ),
    ).toBe(true);
    expect(
      agentInstructionSaveIsPending(
        pending,
        { id: pending.assetId, version: 2 },
        version,
      ),
    ).toBe(true);
    expect(
      agentInstructionSaveIsPending(
        pending,
        { id: pending.assetId, version: 3 },
        version,
      ),
    ).toBe(false);
  });

  test("a 409 waits for the latest remote version before rebasing local edits", () => {
    const conflict = {
      assetId: version.agent_id,
      assetVersion: 2,
      versionId: version.id,
    };
    const newerVersion = {
      ...version,
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    };

    expect(
      agentInstructionConflictHasLatestServerState(
        conflict,
        {
          id: version.agent_id,
          version: 3,
          current_published_version_id: newerVersion.id,
        },
        version,
      ),
    ).toBe(false);
    expect(
      agentInstructionConflictHasLatestServerState(
        conflict,
        {
          id: version.agent_id,
          version: 3,
          current_published_version_id: newerVersion.id,
        },
        newerVersion,
      ),
    ).toBe(true);
  });

  test("resolves the current internal revision without exposing revision controls", () => {
    const stalePinned = {
      ...version,
      id: "99999999-9999-4999-8999-999999999999",
      version_number: 2,
      identity: "# Stale pinned identity",
    };

    expect(
      effectiveAssetVersion("system", false, stalePinned, version, stalePinned)
        ?.id,
    ).toBe(version.id);
    expect(
      effectiveAssetVersion("system", true, stalePinned, version, version)?.id,
    ).toBe(stalePinned.id);
  });
});
