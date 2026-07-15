import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AgentSelectorDialog,
  createProjectChatForAgent,
  executableProjectAgents,
  projectThreadAgentSelection,
} from "@/components/projects/private-work/agent-selector-dialog";
import type { ProjectAssetList } from "@/core/shared-assets";

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
        created.push({ scope, input });
      },
      navigate: (path) => navigated.push(path),
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
        },
      },
    ]);
    expect(navigated).toEqual([`/projects/alpha%20team/chats/${threadId}`]);
  });
});
