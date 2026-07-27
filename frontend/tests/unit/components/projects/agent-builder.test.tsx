import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderBlueprintReview } from "@/components/projects/agents/agent-builder-blueprint-review";
import { AgentBuilderProgress } from "@/components/projects/agents/agent-builder-progress";
import { AgentBuilderResumeBanner } from "@/components/projects/agents/agent-builder-resume-banner";
import {
  AgentBuilderStartView,
  agentBuilderErrorMessage,
} from "@/components/projects/agents/agent-builder-start";
import {
  AgentBuilderConversationView,
  agentBuilderSessionPath,
  agentBuilderWorkspaceErrorMessage,
  submitAgentBuilderClarificationMutation,
} from "@/components/projects/agents/agent-builder-workspace";
import {
  AgentBuilderApiError,
  type AgentBuilderBlueprint,
  type AgentBuilderSession,
} from "@/core/agent-builder";
import { I18nProvider } from "@/core/i18n/context";

const blueprint: AgentBuilderBlueprint = {
  description: "生成可靠、可运行的测试。",
  model_ref: "default",
  tool_groups: ["files"],
  skill_version_ids: [],
  mcp_version_ids: [],
  agents_instructions: "# 工作方式\n\n先理解代码，再生成测试。",
  soul: "# 风格\n\n务实、直接。",
  identity: "# 身份\n\n你是一名测试工程师。",
  user_context: "# 用户\n\n用户偏好可运行的结果。",
};

const session: AgentBuilderSession = {
  id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  owner_user_id: "user-1",
  thread_id: "33333333-3333-4333-8333-333333333333",
  slug: "test-engineer",
  display_name: "test-engineer",
  status: "proposal_ready",
  revision: 4,
  blueprint,
  blueprint_checksum: "checksum-1",
  messages: [
    {
      id: "message-1",
      role: "user",
      content: "它负责单元测试和集成测试。",
      created_at: "2026-07-26T08:00:00Z",
    },
    {
      id: "message-2",
      role: "assistant",
      content: "明白了，我已经整理好第一版设计。",
      created_at: "2026-07-26T08:00:01Z",
    },
  ],
  active_clarification: null,
  progress: [
    {
      id: "project-context",
      label: "了解项目现有 Agent",
      status: "completed",
    },
    {
      id: "generate-blueprint",
      label: "生成四项 Agent 设置",
      status: "completed",
    },
  ],
  error_code: null,
  error_message: null,
  created_agent_id: null,
  created_at: "2026-07-26T07:58:00Z",
  updated_at: "2026-07-26T08:00:00Z",
};

describe("Agent Builder UI", () => {
  test("renders a focused one-field naming step with visible validation", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderStartView
        name="Code Reviewer"
        normalizedName="code-reviewer"
        errorMessage="仅支持小写字母、数字和单个连字符"
        pending={false}
        onNameChange={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    expect(html).toContain("给新 Agent 起个名字");
    expect(html).toContain("例如 code-reviewer");
    expect(html).toContain("将保存为");
    expect(html).toContain("code-reviewer");
    expect(html).toContain("仅支持小写字母、数字和单个连字符");
    expect(html).toContain('role="alert"');
    expect(html).toContain("继续");
  });

  test("shows resumable sessions separately from completed Agent cards", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderResumeBanner
        projectSlug="default-project"
        sessions={[
          {
            id: session.id,
            slug: session.slug,
            display_name: session.display_name,
            status: "interviewing",
            updated_at: session.updated_at,
          },
          {
            id: "99999999-9999-4999-8999-999999999999",
            slug: "completed-agent",
            display_name: "completed-agent",
            status: "completed",
            updated_at: session.updated_at,
          },
        ]}
      />,
    );

    expect(html).toContain("继续设计未完成的 Agent");
    expect(html).toContain("test-engineer");
    expect(html).toContain(
      `/projects/default-project/agents/new/${session.id}`,
    );
    expect(html).not.toContain("已创建");
    expect(html).not.toContain("completed-agent");
  });

  test("renders product progress without exposing chain-of-thought language", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderProgress items={session.progress} generating={false} />,
    );

    expect(html).toContain("设计步骤");
    expect(html).toContain("了解项目现有 Agent");
    expect(html).toContain("生成四项 Agent 设置");
    for (const name of ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md"]) {
      expect(html).toContain(name);
    }
    expect(html).not.toContain("思考过程");
    expect(html).not.toContain("Chain of Thought");
  });

  test("previews all four logical documents and the runtime summary", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderBlueprintReview
        blueprint={blueprint}
        canAuthor
        editing={false}
        pending={false}
        creating={false}
        dirty={false}
        canCreate
        selectedField="agents_instructions"
        displayMode="preview"
        errorMessage={null}
        onSelectedFieldChange={() => undefined}
        onDisplayModeChange={() => undefined}
        onBlueprintChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );

    for (const name of ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md"]) {
      expect(html).toContain(name);
    }
    expect(html).toContain("运行配置");
    expect(html).toContain("default");
    expect(html).toContain("创建 Agent");
    expect(html).toContain("创建后默认停用，需手动启用");
  });

  test("blocks Agent creation when a referenced MCP version is not runnable", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderBlueprintReview
        blueprint={{
          ...blueprint,
          mcp_version_ids: ["11111111-1111-4111-8111-111111111111"],
        }}
        canAuthor
        editing={false}
        pending={false}
        creating={false}
        dirty={false}
        canCreate
        mcpDependencyBlockReason="该 MCP 版本当前不能作为 Agent 依赖"
        selectedField="agents_instructions"
        displayMode="preview"
        errorMessage={null}
        onSelectedFieldChange={() => undefined}
        onDisplayModeChange={() => undefined}
        onBlueprintChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );

    expect(html).toContain("该 MCP 版本当前不能作为 Agent 依赖");
    expect(html).toContain('role="alert"');
    expect(html).toContain("创建 Agent");
    expect(html).toContain('disabled=""');
  });

  test("does not label document generation as Agent creation", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderBlueprintReview
        blueprint={blueprint}
        canAuthor
        editing={false}
        pending
        creating={false}
        dirty={false}
        canCreate={false}
        selectedField="agents_instructions"
        displayMode="preview"
        errorMessage={null}
        onSelectedFieldChange={() => undefined}
        onDisplayModeChange={() => undefined}
        onBlueprintChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );

    expect(html).toContain("创建 Agent");
    expect(html).not.toContain("正在创建…");
  });

  test("keeps conversation, clarification and final proposal in one scroll surface", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentBuilderConversationView
          session={{
            ...session,
            status: "awaiting_clarification",
            active_clarification: {
              version: 1,
              kind: "human_input_request",
              source: "ask_clarification",
              request_id: "clarification-1",
              title: "需要你的帮助",
              question: "它更偏向生成测试，还是执行测试？",
              input_mode: "choice_with_other",
              options: [
                { id: "generate", label: "生成测试", value: "生成测试" },
                { id: "execute", label: "执行测试", value: "执行测试" },
              ],
            },
          }}
          composerText=""
          canAuthor
          mutationPending={false}
          commitPending={false}
          blueprintEditing={false}
          blueprintDraft={blueprint}
          blueprintDirty={false}
          selectedField="agents_instructions"
          displayMode="preview"
          errorMessage={null}
          onComposerTextChange={() => undefined}
          onSubmitMessage={() => undefined}
          onSubmitClarification={() => undefined}
          onSelectedFieldChange={() => undefined}
          onDisplayModeChange={() => undefined}
          onBlueprintChange={() => undefined}
          onBlueprintEdit={() => undefined}
          onBlueprintSave={() => undefined}
          onBlueprintDiscard={() => undefined}
          onComplete={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain("它负责单元测试和集成测试。");
    expect(html).toContain("明白了，我已经整理好第一版设计。");
    expect(html).toContain("它更偏向生成测试，还是执行测试？");
    expect(html).toContain("生成测试");
    expect(html).toContain("执行测试");
    expect(html).toContain("等待你回答上方问题");
    expect(html).toContain('aria-disabled="true"');
  });

  test("locks new turns while the generated blueprint has local edits", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentBuilderConversationView
          session={session}
          composerText="继续改身份"
          canAuthor
          mutationPending={false}
          commitPending={false}
          blueprintEditing
          blueprintDraft={blueprint}
          blueprintDirty
          selectedField="identity"
          displayMode="source"
          errorMessage={null}
          onComposerTextChange={() => undefined}
          onSubmitMessage={() => undefined}
          onSubmitClarification={() => undefined}
          onSelectedFieldChange={() => undefined}
          onDisplayModeChange={() => undefined}
          onBlueprintChange={() => undefined}
          onBlueprintEdit={() => undefined}
          onBlueprintSave={() => undefined}
          onBlueprintDiscard={() => undefined}
          onComplete={() => undefined}
        />
      </I18nProvider>,
    );
    const composerStart = html.indexOf('aria-label="描述想要的 Agent"');
    const composerEnd = html.indexOf("</textarea>", composerStart);
    const composer = html.slice(composerStart, composerEnd);

    expect(composerStart).toBeGreaterThan(-1);
    expect(composer).toContain('disabled=""');
    expect(composer).toContain("请先保存或放弃上方修改");
  });

  test("remounts clarification input per request and retains failed answers", async () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/agents/agent-builder-workspace.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("key={session.active_clarification.request_id}");

    let resolved = 0;
    await expect(
      submitAgentBuilderClarificationMutation(
        async () => {
          throw new Error("network");
        },
        () => {
          resolved += 1;
        },
      ),
    ).resolves.toBe(false);
    expect(resolved).toBe(0);
    await expect(
      submitAgentBuilderClarificationMutation(
        async () => ({
          data: { status: "failed" as const },
        }),
        () => {
          resolved += 1;
        },
      ),
    ).resolves.toBe(false);
    expect(resolved).toBe(0);
    await expect(
      submitAgentBuilderClarificationMutation(
        async () => ({
          data: { status: "proposal_ready" as const },
        }),
        () => {
          resolved += 1;
        },
      ),
    ).resolves.toBe(true);
    expect(resolved).toBe(1);
  });

  test("renders an owner session read-only when Agent authoring permission is absent", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentBuilderConversationView
          session={session}
          composerText="不应提交"
          canAuthor={false}
          mutationPending={false}
          commitPending={false}
          blueprintEditing={false}
          blueprintDraft={blueprint}
          blueprintDirty={false}
          selectedField="identity"
          displayMode="source"
          errorMessage={null}
          onComposerTextChange={() => undefined}
          onSubmitMessage={() => undefined}
          onSubmitClarification={() => undefined}
          onSelectedFieldChange={() => undefined}
          onDisplayModeChange={() => undefined}
          onBlueprintChange={() => undefined}
          onBlueprintEdit={() => undefined}
          onBlueprintSave={() => undefined}
          onBlueprintDiscard={() => undefined}
          onComplete={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain("当前账号没有继续设计 Agent 的权限");
    expect(html).not.toContain("编辑指令");
    expect(html).not.toContain(">创建 Agent</button>");
    expect(html).not.toContain('aria-label="描述想要的 Agent"');
  });

  test("routes every Builder mutation through semantic idempotency and protects dirty navigation", () => {
    const workspaceSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/agents/agent-builder-workspace.tsx",
      ),
      "utf8",
    );
    const startSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/agents/agent-builder-start.tsx",
      ),
      "utf8",
    );
    const combined = `${startSource}\n${workspaceSource}`;

    for (const channel of [
      '"create"',
      '"message-turn"',
      '"clarification-turn"',
      '"blueprint-turn"',
      '"commit"',
      '"cancel"',
    ]) {
      expect(combined).toContain(channel);
    }
    expect(combined).not.toContain("idempotency_key: crypto.randomUUID()");
    expect(workspaceSource).toContain('"beforeunload"');
    expect(workspaceSource).toContain(
      'document.addEventListener("click", preventDirtyNavigation, true)',
    );
    expect(workspaceSource).toContain("放弃修改并离开");
  });

  test("builds an encoded session route under the active Agent section", () => {
    expect(agentBuilderSessionPath("default project", session.id)).toBe(
      `/projects/default%20project/agents/new/${session.id}`,
    );
  });

  test("maps duplicate, unavailable, malformed and uncertain commit failures", () => {
    expect(
      agentBuilderErrorMessage(
        new AgentBuilderApiError(409, "AGENT_BUILDER_CONFLICT", "conflict"),
      ),
    ).toContain("已存在同名 Agent");
    expect(
      agentBuilderErrorMessage(
        new AgentBuilderApiError(
          503,
          "AGENT_BUILDER_UNAVAILABLE",
          "模型暂时不可用",
        ),
      ),
    ).toContain("模型暂时不可用");
    expect(
      agentBuilderErrorMessage(
        new AgentBuilderApiError(
          200,
          "AGENT_BUILDER_RESPONSE_INVALID",
          "invalid",
        ),
      ),
    ).toContain("格式异常");
    expect(
      agentBuilderWorkspaceErrorMessage(
        new AgentBuilderApiError(0, "AGENT_BUILDER_NETWORK_ERROR", "network"),
        true,
      ),
    ).toContain("请勿重复创建");
  });
});
