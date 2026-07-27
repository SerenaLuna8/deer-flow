import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AgentSelectorDialog,
  agentMcpDependencyAvailability,
  createProjectChatForAgent,
  enableSystemAgentAndCreateProjectChat,
  ensureMainSystemAgentBindings,
  executableProjectAgents,
  configurableSystemAgents,
  mainProjectAgent,
  projectAgentsStartChatPath,
  systemAgentDependencyAvailability,
  projectThreadAgentSelection,
} from "@/components/projects/private-work/agent-selector-dialog";
import type {
  AssetVersion,
  ProjectAssetItem,
  ProjectAssetList,
  VersionHistoryResponse,
} from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_AGENT_ID = "22222222-2222-4222-8222-222222222222";
const SYSTEM_AGENT_ID = "33333333-3333-4333-8333-333333333333";
const VERSION_ID = "44444444-4444-4444-8444-444444444444";

const base = {
  slug: "analyst",
  display_name: "Analyst",
  status: "active" as const,
  current_published_version_id: VERSION_ID,
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-15T00:00:00Z",
  updated_at: "2026-07-15T00:00:00Z",
};

const catalog: ProjectAssetList = {
  project_items: [
    {
      ...base,
      id: PROJECT_AGENT_ID,
      scope: "project",
      project_id: PROJECT_ID,
      capabilities: ["shared_assets.read", "shared_assets.execute"],
      binding: null,
    },
    {
      ...base,
      id: "55555555-5555-4555-8555-555555555555",
      scope: "project",
      project_id: PROJECT_ID,
      status: "suspended",
      capabilities: ["shared_assets.read", "shared_assets.execute"],
      binding: null,
    },
    {
      ...base,
      id: "66666666-6666-4666-8666-666666666666",
      scope: "project",
      project_id: PROJECT_ID,
      current_published_version_id: null,
      capabilities: ["shared_assets.read", "shared_assets.execute"],
      binding: null,
    },
  ],
  system_items: [
    {
      ...base,
      id: SYSTEM_AGENT_ID,
      scope: "system",
      project_id: null,
      capabilities: ["shared_assets.read", "shared_assets.execute"],
      binding: {
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: SYSTEM_AGENT_ID,
        version_id: VERSION_ID,
        enabled: true,
        version: 1,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: "2026-07-15T00:00:00Z",
        updated_at: "2026-07-15T00:00:00Z",
      },
    },
    {
      ...base,
      id: "77777777-7777-4777-8777-777777777777",
      scope: "system",
      project_id: null,
      capabilities: ["shared_assets.read", "shared_assets.execute"],
      binding: {
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: "77777777-7777-4777-8777-777777777777",
        version_id: VERSION_ID,
        enabled: false,
        version: 1,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: "2026-07-15T00:00:00Z",
        updated_at: "2026-07-15T00:00:00Z",
      },
    },
  ],
  request_id: "req-agents",
};

describe("project Agent selector", () => {
  test("selects the active packaged Main Agent before or after project binding", () => {
    const mainCatalog: ProjectAssetList = {
      ...catalog,
      system_items: [
        {
          ...catalog.system_items[0]!,
          slug: "project-assistant",
          display_name: "Main",
        },
        {
          ...catalog.system_items[1]!,
          slug: "other-system-agent",
        },
      ],
    };

    expect(mainProjectAgent(mainCatalog)?.display_name).toBe("Main");
    expect(
      mainProjectAgent({
        ...mainCatalog,
        system_items: mainCatalog.system_items.map((item) => ({
          ...item,
          binding: item.binding
            ? { ...item.binding, enabled: false }
            : item.binding,
        })),
      })?.display_name,
    ).toBe("Main");
  });

  test("shows only active executable project Agents and enabled system bindings", () => {
    const agents = executableProjectAgents(catalog);

    expect(agents.map((agent) => agent.id)).toEqual([
      PROJECT_AGENT_ID,
      SYSTEM_AGENT_ID,
    ]);
    const html = renderToStaticMarkup(
      <AgentSelectorDialog
        open
        agents={agents}
        isCreating={false}
        onOpenChange={() => undefined}
        onSelect={() => undefined}
      />,
    );
    expect(html.match(/Analyst/g)).toHaveLength(2);
    expect(html).toContain("项目 Agent");
    expect(html).toContain("系统 Agent");
    expect(html).toContain("选择一个 Agent 开始新的私有对话");
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain('data-testid="project-agent-selector-overlay"');
    expect(html).toContain("data-dialog-initial-focus");
    expect(html).not.toMatch(/logical|复核版本/u);
  });

  test("create selection contains logical Agent only", () => {
    expect(
      projectThreadAgentSelection(executableProjectAgents(catalog)[0]!),
    ).toEqual({
      agentAssetId: PROJECT_AGENT_ID,
      agentScope: "project",
    });
  });

  test("creates an explicit UUID thread then navigates to project detail", async () => {
    const created: unknown[] = [];
    const navigated: string[] = [];
    const events: string[] = [];
    const agent = executableProjectAgents(catalog)[0]!;
    const threadId = "88888888-8888-4888-8888-888888888888";

    await createProjectChatForAgent({
      scope: {
        accountId: "99999999-9999-4999-8999-999999999999",
        projectId: PROJECT_ID,
      },
      projectSlug: "alpha team",
      agent,
      createThreadId: () => threadId,
      createThread: async (scope, input) => {
        events.push("created");
        created.push({ scope, input });
      },
      invalidateThreadLists: () => events.push("invalidated"),
      navigate: (path) => {
        events.push("navigated");
        navigated.push(path);
      },
    });

    expect(created).toEqual([
      {
        scope: {
          accountId: "99999999-9999-4999-8999-999999999999",
          projectId: PROJECT_ID,
        },
        input: {
          threadId,
          agentAssetId: PROJECT_AGENT_ID,
          agentScope: "project",
          displayName: "新对话",
        },
      },
    ]);
    expect(events).toEqual(["created", "invalidated", "navigated"]);
    expect(navigated).toEqual([`/projects/alpha%20team/chats/${threadId}`]);
  });

  test("keeps no-Agent guidance in context for Admin Editor and Runner capabilities", () => {
    const configurable: ProjectAssetItem = {
      ...catalog.system_items[1]!,
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
    };
    const agentsPath = projectAgentsStartChatPath("alpha team", "intent-admin");
    const admin = renderToStaticMarkup(
      <AgentSelectorDialog
        open
        agents={[]}
        configurableSystemAgents={[configurable]}
        canAuthorProjectAgent
        agentsPath={agentsPath}
        isCreating={false}
        onOpenChange={() => undefined}
        onSelect={() => undefined}
        onEnableSystemAgent={() => undefined}
      />,
    );
    expect(admin).toContain("项目还没有可执行 Agent");
    expect(admin).toContain("启用 Analyst 并开始对话");
    expect(admin).toContain("创建项目 Agent");
    expect(admin).toContain(
      'href="/projects/alpha%20team/agents?intent=start_chat&amp;intent_id=intent-admin"',
    );

    const editor = renderToStaticMarkup(
      <AgentSelectorDialog
        open
        agents={[]}
        configurableSystemAgents={[]}
        canAuthorProjectAgent
        agentsPath={agentsPath}
        isCreating={false}
        onOpenChange={() => undefined}
        onSelect={() => undefined}
      />,
    );
    expect(editor).toContain("创建项目 Agent");
    expect(editor).not.toContain("启用 Analyst");

    const runner = renderToStaticMarkup(
      <AgentSelectorDialog
        open
        agents={[]}
        configurableSystemAgents={[]}
        canAuthorProjectAgent={false}
        agentsPath={agentsPath}
        isCreating={false}
        onOpenChange={() => undefined}
        onSelect={() => undefined}
      />,
    );
    expect(runner).toContain("请联系项目 Admin 或 Editor 完成配置");
    expect(runner).not.toContain("创建项目 Agent");
    expect(runner).not.toContain("启用并开始对话");
  });

  test("finds only system Agents the current capability set can enable", () => {
    const disabled = catalog.system_items[1]!;
    const configurableCatalog: ProjectAssetList = {
      ...catalog,
      system_items: [
        catalog.system_items[0]!,
        {
          ...disabled,
          capabilities: [
            "shared_assets.read",
            "shared_assets.execute",
            "shared_assets.manage_bindings",
          ],
        },
      ],
    };

    expect(
      configurableSystemAgents(configurableCatalog).map(({ id }) => id),
    ).toEqual([disabled.id]);
  });

  test("only offers immediate enable when the published Agent dependencies are already bound", () => {
    const agent = {
      ...catalog.system_items[1]!,
      current_published_version_id: VERSION_ID,
    };
    const skillVersionId = "99999999-9999-4999-8999-999999999991";
    const mcpVersionId = "99999999-9999-4999-8999-999999999992";
    const history = {
      request_id: "req-history",
      data: [
        {
          id: VERSION_ID,
          agent_id: agent.id,
          version_number: 1,
          workflow_status: "published",
          description: "Project assistant",
          agents_instructions: "",
          soul: "Careful assistant",
          identity: "",
          user_context: "",
          payload_schema_version: 2,
          model_ref: "default",
          tool_groups: [],
          skill_version_ids: [skillVersionId],
          mcp_version_ids: [mcpVersionId],
          supersedes_version_id: null,
          payload_checksum: "sha256:test",
          created_by_user_id: "user-1",
          created_at: "2026-07-15T00:00:00Z",
        },
      ],
    } satisfies VersionHistoryResponse;

    expect(
      systemAgentDependencyAvailability(
        agent,
        undefined,
        new Set([skillVersionId]),
        new Set([mcpVersionId]),
      ),
    ).toBe("loading");
    expect(
      systemAgentDependencyAvailability(
        agent,
        history,
        new Set([skillVersionId]),
        new Set(),
      ),
    ).toBe("blocked");
    expect(
      systemAgentDependencyAvailability(
        agent,
        history,
        new Set([skillVersionId]),
        new Set([mcpVersionId]),
      ),
    ).toBe("ready");
  });

  test("blocks chat selection when an Agent references an unsupported MCP version", () => {
    const agent = catalog.project_items[0]!;
    const mcpVersionId = "99999999-9999-4999-8999-999999999992";
    const history = {
      request_id: "req-history",
      data: [
        {
          id: VERSION_ID,
          agent_id: agent.id,
          version_number: 1,
          workflow_status: "published",
          description: "Project assistant",
          agents_instructions: "",
          soul: "",
          identity: "",
          user_context: "",
          payload_schema_version: 2,
          model_ref: "default",
          tool_groups: [],
          skill_version_ids: [],
          mcp_version_ids: [mcpVersionId],
          supersedes_version_id: null,
          payload_checksum: "sha256:test",
          created_by_user_id: "user-1",
          created_at: "2026-07-15T00:00:00Z",
        },
      ],
    } satisfies VersionHistoryResponse;
    const unsupportedMcp = {
      id: mcpVersionId,
      mcp_server_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      version_number: 1,
      workflow_status: "published",
      definition: {
        description: "Local MCP",
        transport: "stdio",
        command: "server",
        args: [],
        url: null,
        env: {},
        headers: {},
        oauth: {},
        routing: {},
        tool_overrides: {},
        timeout_seconds: 30,
        credential_slots: [],
      },
      credential_slots: [],
      credential_grants: [],
      supersedes_version_id: null,
      payload_checksum: "sha256:mcp",
      submitted_at: null,
      reviewed_at: null,
      reviewed_by_user_id: null,
      created_by_user_id: "user-1",
      created_at: "2026-07-15T00:00:00Z",
    } satisfies AssetVersion;

    expect(
      agentMcpDependencyAvailability(agent, history, [
        { scope: "project", version: unsupportedMcp },
      ]),
    ).toBe("blocked");
    expect(
      agentMcpDependencyAvailability(agent, history, [
        {
          scope: "project",
          version: {
            ...unsupportedMcp,
            definition: {
              ...unsupportedMcp.definition,
              transport: "http",
              command: null,
              url: "https://mcp.example.test",
            },
          },
        },
      ]),
    ).toBe("ready");
  });

  test("keeps dependency-blocked system Agents out of the immediate enable action", () => {
    const configurable: ProjectAssetItem = {
      ...catalog.system_items[1]!,
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
    };
    const html = renderToStaticMarkup(
      <AgentSelectorDialog
        open
        agents={[]}
        configurableSystemAgents={[]}
        blockedSystemAgents={[configurable]}
        canAuthorProjectAgent
        agentsPath={projectAgentsStartChatPath("alpha", "dependency-check")}
        isCreating={false}
        onOpenChange={() => undefined}
        onSelect={() => undefined}
      />,
    );

    expect(html).toContain("需要先完成依赖配置");
    expect(html).toContain("Analyst");
    expect(html).not.toContain("启用 Analyst 并开始对话");
    expect(html).toContain("前往 Agent 页面完成配置");
  });

  test("explains blocked MCP dependencies even when another Agent is runnable", () => {
    const ready = catalog.project_items[0]!;
    const blocked = {
      ...catalog.project_items[0]!,
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      display_name: "Legacy MCP Agent",
    };
    const html = renderToStaticMarkup(
      <AgentSelectorDialog
        open
        agents={[ready]}
        blockedRuntimeAgents={[blocked]}
        isCreating={false}
        onOpenChange={() => undefined}
        onSelect={() => undefined}
      />,
    );

    expect(html).toContain("Analyst");
    expect(html).toContain("Legacy MCP Agent");
    expect(html).toContain("MCP 依赖当前不可运行");
    expect(html).toContain("已阻止开始对话");
  });

  test("enables a pinned system Agent before creating and entering the first Chat", async () => {
    const agent: ProjectAssetItem = {
      ...catalog.system_items[1]!,
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
    };
    const calls: unknown[] = [];
    const threadId = "88888888-8888-4888-8888-888888888888";

    await enableSystemAgentAndCreateProjectChat({
      scope: {
        accountId: "99999999-9999-4999-8999-999999999999",
        projectId: PROJECT_ID,
      },
      projectSlug: "alpha",
      agent,
      enableBinding: async (input) => {
        calls.push(["enable", input]);
      },
      createThreadId: () => threadId,
      createThread: async (_scope, input) => {
        calls.push(["create", input]);
      },
      navigate: (path) => calls.push(["navigate", path]),
    });

    expect(calls).toEqual([
      [
        "enable",
        {
          asset_id: agent.id,
          version_id: VERSION_ID,
          expected_binding_version: 1,
        },
      ],
      [
        "create",
        {
          threadId,
          agentAssetId: agent.id,
          agentScope: "system",
          displayName: "新对话",
        },
      ],
      ["navigate", `/projects/alpha/chats/${threadId}`],
    ]);
  });

  test("enables Main system dependencies before enabling Main", async () => {
    const mainAgent: ProjectAssetItem = {
      ...catalog.system_items[1]!,
      slug: "project-assistant",
      display_name: "Main",
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
      binding: null,
    };
    const skillVersionId = "99999999-9999-4999-8999-999999999999";
    const systemSkill: ProjectAssetItem = {
      ...catalog.system_items[1]!,
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      slug: "deerflow-core",
      current_published_version_id: skillVersionId,
      binding: null,
    };
    const calls: unknown[] = [];

    await ensureMainSystemAgentBindings({
      agent: mainAgent,
      requiredSkillVersionIds: [skillVersionId],
      requiredMcpVersionIds: [],
      skillDependencies: [
        {
          item: systemSkill,
          versionId: skillVersionId,
          versionNumber: 1,
          boundVersionNumber: null,
        },
      ],
      mcpDependencies: [],
      enableBinding: async (kind, input) => {
        calls.push([kind, input]);
      },
      moveBinding: async () => undefined,
    });

    expect(calls).toEqual([
      [
        "skill",
        {
          asset_id: systemSkill.id,
          version_id: skillVersionId,
        },
      ],
      [
        "agent",
        {
          asset_id: mainAgent.id,
          version_id: VERSION_ID,
        },
      ],
    ]);
  });

  test("repairs a mismatched dependency even when Main is already bound", async () => {
    const mainAgent: ProjectAssetItem = {
      ...catalog.system_items[0]!,
      slug: "project-assistant",
    };
    const targetVersionId = "99999999-9999-4999-8999-999999999999";
    const boundVersionId = "88888888-8888-4888-8888-888888888888";
    const systemSkill: ProjectAssetItem = {
      ...catalog.system_items[0]!,
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      current_published_version_id: targetVersionId,
      binding: {
        ...catalog.system_items[0]!.binding!,
        kind: "skill",
        asset_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        version_id: boundVersionId,
        version: 3,
      },
    };
    const moves: unknown[] = [];
    const changed = await ensureMainSystemAgentBindings({
      agent: mainAgent,
      requiredSkillVersionIds: [targetVersionId],
      requiredMcpVersionIds: [],
      skillDependencies: [
        {
          item: systemSkill,
          versionId: targetVersionId,
          versionNumber: 2,
          boundVersionNumber: 1,
        },
      ],
      mcpDependencies: [],
      enableBinding: async () => {
        throw new Error("already-bound Main must not be enabled again");
      },
      moveBinding: async (kind, assetId, action, input) => {
        moves.push([kind, assetId, action, input]);
      },
    });

    expect(changed).toBe(true);
    expect(moves).toEqual([
      [
        "skill",
        systemSkill.id,
        "upgrade",
        {
          version_id: targetVersionId,
          expected_binding_version: 3,
        },
      ],
    ]);
  });

  test("does not bind Main when a required MCP version is unsupported", async () => {
    const mainAgent: ProjectAssetItem = {
      ...catalog.system_items[1]!,
      slug: "project-assistant",
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
      binding: null,
    };
    const mcpVersionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const systemMcp: ProjectAssetItem = {
      ...catalog.system_items[1]!,
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      current_published_version_id: mcpVersionId,
      binding: null,
    };
    const calls: unknown[] = [];

    await expect(
      ensureMainSystemAgentBindings({
        agent: mainAgent,
        requiredSkillVersionIds: [],
        requiredMcpVersionIds: [mcpVersionId],
        skillDependencies: [],
        mcpDependencies: [
          {
            item: systemMcp,
            versionId: mcpVersionId,
            versionNumber: 1,
            boundVersionNumber: null,
          },
        ],
        mcpVersions: [
          {
            scope: "system",
            version: {
              id: mcpVersionId,
              mcp_server_id: systemMcp.id,
              version_number: 1,
              workflow_status: "published",
              definition: {
                description: "Legacy",
                transport: "streamable_http",
                command: null,
                args: [],
                url: "https://mcp.example.test",
                env: {},
                headers: {},
                oauth: {},
                routing: {},
                tool_overrides: {},
                timeout_seconds: 30,
                credential_slots: [],
              },
              credential_slots: [],
              credential_grants: [],
              supersedes_version_id: null,
              payload_checksum: "sha256:mcp",
              submitted_at: null,
              reviewed_at: null,
              reviewed_by_user_id: null,
              created_by_user_id: "user-1",
              created_at: "2026-07-15T00:00:00Z",
            },
          },
        ],
        enableBinding: async (kind, input) => {
          calls.push([kind, input]);
        },
        moveBinding: async () => undefined,
      }),
    ).rejects.toThrow("不能作为 Agent 依赖");
    expect(calls).toEqual([]);
  });
});
