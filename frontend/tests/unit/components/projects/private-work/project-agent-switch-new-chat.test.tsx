import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AgentSelectorDialog,
  createProjectChatForAgent,
  otherProjectAgents,
  projectAgentCreatePath,
} from "@/components/projects/private-work/agent-selector-dialog";
import { ThreadAgentIndicator } from "@/components/workspace/thread-agent-indicator";
import { I18nProvider } from "@/core/i18n/context";
import { zhCN } from "@/core/i18n/locales/zh-CN";
import type { ProjectClientScope } from "@/core/private-work/types";
import type { ProjectAssetItem } from "@/core/shared-assets";

const ACCOUNT_ID = "00000000-0000-4000-8000-000000000001";
const PROJECT_ID = "00000000-0000-4000-8000-000000000002";
const CURRENT_AGENT_ID = "00000000-0000-4000-8000-000000000003";
const OTHER_AGENT_ID = "00000000-0000-4000-8000-000000000004";
const VERSION_ID = "00000000-0000-4000-8000-000000000005";
const NEW_THREAD_ID = "00000000-0000-4000-8000-000000000006";
const CURRENT_THREAD_ID = "00000000-0000-4000-8000-000000000007";

function agent(
  id: string,
  scope: "project" | "system",
  displayName: string,
): ProjectAssetItem {
  return {
    id,
    project_id: scope === "project" ? PROJECT_ID : null,
    scope,
    slug: displayName.toLocaleLowerCase(),
    display_name: displayName,
    description: null,
    status: "active",
    current_published_version_id: VERSION_ID,
    version: 1,
    created_by_user_id: ACCOUNT_ID,
    created_at: "2026-08-13T00:00:00Z",
    updated_at: "2026-08-13T00:00:00Z",
    capabilities: ["private_work.create", "shared_assets.execute"],
    binding: null,
  };
}

describe("use another Agent for a new project chat", () => {
  test("excludes only the Agent pinned to the current Thread", () => {
    const current = agent(CURRENT_AGENT_ID, "project", "Current");
    const other = agent(OTHER_AGENT_ID, "project", "Other");
    const sameIdDifferentScope = agent(CURRENT_AGENT_ID, "system", "System");

    expect(
      otherProjectAgents([current, other, sameIdDifferentScope], {
        agentAssetId: CURRENT_AGENT_ID,
        agentScope: "project",
      }).map((item) => `${item.scope}:${item.id}`),
    ).toEqual([`project:${OTHER_AGENT_ID}`, `system:${CURRENT_AGENT_ID}`]);
  });

  test("creates a distinct server-authoritative Thread and never updates the current one", async () => {
    const scope: ProjectClientScope = {
      accountId: ACCOUNT_ID,
      projectId: PROJECT_ID,
    };
    const selected = agent(OTHER_AGENT_ID, "project", "Other");
    const requests: unknown[] = [];
    const navigations: string[] = [];

    const result = await createProjectChatForAgent({
      scope,
      projectSlug: "alpha project",
      agent: selected,
      threadDisplayName: "新对话",
      createThreadId: () => NEW_THREAD_ID,
      createThread: async (receivedScope, input) => {
        requests.push({ scope: receivedScope, input });
        return {};
      },
      navigate: (path) => navigations.push(path),
    });

    expect(result).toBe(NEW_THREAD_ID);
    expect(requests).toEqual([
      {
        scope,
        input: {
          threadId: NEW_THREAD_ID,
          agentAssetId: OTHER_AGENT_ID,
          agentScope: "project",
          displayName: "新对话",
        },
      },
    ]);
    expect(navigations).toEqual([
      `/projects/alpha%20project/chats/${NEW_THREAD_ID}`,
    ]);
    expect(JSON.stringify(requests)).not.toContain(CURRENT_THREAD_ID);
    expect(navigations[0]).not.toContain(CURRENT_THREAD_ID);
  });

  test("labels the current Agent action as creating a new chat", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <ThreadAgentIndicator
          identity={{ displayName: "Reviewer", available: true }}
          onStartNewChat={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain("<button");
    expect(html).toContain(
      'aria-label="用其他 Agent 新建对话；当前 Agent：Reviewer"',
    );
    expect(html).not.toContain("替换当前");
  });

  test("describes an empty alternate list without declaring the working Agent unavailable", () => {
    const copy = zhCN.agents.selector;
    const agentsPath = projectAgentCreatePath("alpha project");
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentSelectorDialog
          open
          agents={[]}
          canAuthorProjectAgent
          agentsPath={agentsPath}
          title={copy.alternateTitle}
          description={copy.alternateDescription}
          emptyTitle={copy.alternateEmptyTitle}
          emptyDescription={copy.alternateEmptyDescription}
          isCreating={false}
          onOpenChange={() => undefined}
          onSelect={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain("选择另一个可用 Agent 新建对话。");
    expect(html).toContain("当前没有可用于新对话的其他 Agent。");
    expect(html).not.toContain("默认 Agent 不可用");
    expect(html).not.toContain("请修复默认 Agent");
    expect(html).toContain('href="/projects/alpha%20project/agents/new"');
    expect(html).not.toContain("intent=start_chat");
  });
});
