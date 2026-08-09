import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderBlueprintReview } from "@/components/projects/agents/agent-builder-blueprint-review";
import { AgentBuilderConversationView } from "@/components/projects/agents/agent-builder-workspace";
import type {
  AgentBuilderBlueprint,
  AgentBuilderSession,
} from "@/core/agent-builder";
import { I18nProvider } from "@/core/i18n/context";
import type { Model } from "@/core/models/types";

const TIMESTAMP = "2026-08-07T00:00:00Z";

function blueprint(): AgentBuilderBlueprint {
  return {
    description: "审查代码并给出可执行建议",
    model_ref: "default",
    tool_groups: ["read"],
    skill_version_ids: [],
    mcp_version_ids: [],
    agents_instructions: "# AGENTS.md\n\n审查代码。",
    soul: "# SOUL.md\n\n严谨。",
    identity: "# IDENTITY.md\n\n代码审查 Agent。",
    user_context: "# USER.md\n\n偏好中文。",
  };
}

function session(): AgentBuilderSession {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    project_id: "22222222-2222-4222-8222-222222222222",
    owner_user_id: "user-1",
    thread_id: "33333333-3333-4333-8333-333333333333",
    slug: "code-review",
    display_name: "代码审查",
    status: "interviewing",
    revision: 1,
    blueprint: null,
    blueprint_checksum: null,
    messages: [],
    active_clarification: null,
    active_clarifications: [],
    progress: [],
    error_code: null,
    error_message: null,
    created_agent_id: null,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  };
}

function clarificationSession(): AgentBuilderSession {
  const questions: AgentBuilderSession["active_clarifications"] = [
    {
      version: 1,
      kind: "human_input_request",
      source: "agent_builder",
      request_id: "scope",
      clarification_type: "agent_design",
      title: "问题 1/3",
      question: "主要审查哪些语言和代码类型？",
      context: "明确职责范围",
      input_mode: "choice_with_other",
      options: [
        { id: "scope-1", label: "全面覆盖", value: "全面覆盖" },
        { id: "scope-2", label: "关键风险", value: "关键风险" },
        { id: "scope-3", label: "指定范围", value: "指定范围" },
      ],
    },
  ];
  return {
    ...session(),
    status: "awaiting_clarification",
    active_clarification: questions[0] ?? null,
    active_clarifications: questions,
  };
}

const MODELS: Model[] = [
  {
    name: "gpt-5.6-luna",
    model: "gpt-5.6-luna",
    display_name: "GPT-5.6 Luna",
    description: "通用模型",
    supports_thinking: true,
    supports_reasoning_effort: true,
    supports_vision: true,
    is_default: true,
  },
];

describe("Agent Builder workspace", () => {
  test("renders only the current dynamic question as the chat human-input card", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentBuilderConversationView
          session={clarificationSession()}
          composerText=""
          models={MODELS}
          selectedGenerationModelName="gpt-5.6-luna"
          canAuthor
          mutationPending={false}
          commitPending={false}
          blueprintEditing={false}
          blueprintDraft={null}
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

    expect(html).toContain("问题 1/3");
    expect(html).toContain("主要审查哪些语言和代码类型？");
    expect(html).toContain("全面覆盖");
    expect(html).toContain("关键风险");
    expect(html).toContain("指定范围");
    expect(html.match(/data-testid="human-input-card"/g)).toHaveLength(1);
    expect(html).not.toContain("问题 2/3");
    expect(html).not.toContain("问题 3/3");
    expect(html).not.toContain("完善 Agent 职责");
  });

  test("renders the submitted user message while generation is still pending", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderConversationView
        session={session()}
        composerText=""
        pendingUserMessage="请重点检查并发安全"
        canAuthor
        mutationPending
        commitPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
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
      />,
    );

    expect(html).toContain("请重点检查并发安全");
    expect(html).toContain("正在设计 Agent");
  });

  test("offers a model picker for the Builder conversation", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderConversationView
        session={session()}
        composerText=""
        models={MODELS}
        modelsLoading={false}
        modelsError={null}
        selectedGenerationModelName="gpt-5.6-luna"
        canAuthor
        mutationPending={false}
        commitPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        selectedField="agents_instructions"
        displayMode="preview"
        errorMessage={null}
        onModelsRetry={() => undefined}
        onGenerationModelChange={() => undefined}
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
      />,
    );

    expect(html).toContain('aria-label="选择创建 Agent 的对话模型"');
    expect(html).toContain("GPT-5.6 Luna");
  });

  test("keeps the created Agent runtime model read-only in the blueprint", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderBlueprintReview
        blueprint={blueprint()}
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

    expect(html).toContain("default");
    expect(html).not.toContain('aria-label="选择 Agent 模型"');
  });

  test("keeps the create action in document flow above the composer", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderBlueprintReview
        blueprint={blueprint()}
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

    expect(html).toContain("创建后默认停用，需手动启用");
    expect(html).not.toContain("sticky bottom-3");
  });

  test("keeps the composer outside the scrolling conversation region", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderConversationView
        session={session()}
        composerText=""
        models={MODELS}
        selectedGenerationModelName="gpt-5.6-luna"
        canAuthor
        mutationPending={false}
        commitPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
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
      />,
    );

    const scrollRegion = html.indexOf(
      'data-agent-builder-scroll-region="true"',
    );
    const composerShell = html.indexOf(
      'data-agent-builder-composer-shell="true"',
    );
    expect(scrollRegion).toBeGreaterThanOrEqual(0);
    expect(composerShell).toBeGreaterThan(scrollRegion);
    expect(html).toContain("min-h-0 flex-1 overflow-y-auto overscroll-contain");
    expect(html).toContain("shrink-0 border-t");
    expect(html).not.toContain("sticky bottom-0");
  });
});
