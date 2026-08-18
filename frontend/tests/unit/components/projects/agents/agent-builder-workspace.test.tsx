import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderBlueprintReview } from "@/components/projects/agents/agent-builder-blueprint-review";
import {
  AgentBuilderResumeBannerView,
  resolveAgentBuilderDeleteTarget,
} from "@/components/projects/agents/agent-builder-resume-banner";
import { agentBuilderErrorMessage } from "@/components/projects/agents/agent-builder-start";
import {
  AgentBuilderConversationView,
  agentBuilderCommitSlugFields,
  rebaseAgentBuilderName,
  rebaseAgentBuilderBlueprint,
  recoverAgentBuilderConflict,
} from "@/components/projects/agents/agent-builder-workspace";
import {
  AgentBuilderApiError,
  type AgentBuilderBlueprint,
  type AgentBuilderSession,
  type AgentBuilderSessionSummary,
} from "@/core/agent-builder";
import { I18nProvider } from "@/core/i18n/context";
import { enUS, zhCN } from "@/core/i18n/locales";
import type { Model } from "@/core/models/types";

const TIMESTAMP = "2026-08-07T00:00:00Z";
const MODEL_ID = "00000000-0000-4000-8000-000000000204";

function renderAgentUi(node: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

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
    assumptions: [],
    conflicts: [],
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
    name: MODEL_ID,
    model: MODEL_ID,
    display_name: "GPT-5.6 Luna",
    supports_thinking: true,
    supports_reasoning_effort: true,
    supports_vision: true,
    supports_vision_bridge: false,
    is_default: true,
  },
];

describe("Agent Builder workspace", () => {
  test("explains how to recover after reaching the unfinished-session limit", () => {
    const error = new AgentBuilderApiError(
      429,
      "AGENT_DESIGN_SESSION_LIMIT_EXCEEDED",
      "Too many unfinished Agent design sessions",
    );

    expect(agentBuilderErrorMessage(error, zhCN.agents.builder.errors)).toBe(
      "未完成的 Agent 设计已达到上限。请先恢复或取消一个已有设计，再新建设计。",
    );
    expect(agentBuilderErrorMessage(error, enUS.agents.builder.errors)).toBe(
      "You have reached the unfinished Agent design limit. Resume or cancel an existing design before starting another.",
    );
  });

  test("omits the commit slug by default and includes it only for a rename", () => {
    expect(agentBuilderCommitSlugFields("code-review", "code-review")).toEqual(
      {},
    );
    expect(
      agentBuilderCommitSlugFields("available-agent", "code-review"),
    ).toEqual({ slug: "available-agent" });
  });

  test("renders only the current dynamic question as the chat human-input card", () => {
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={clarificationSession()}
        composerText=""
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
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
    const html = renderAgentUi(
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
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={session()}
        composerText=""
        models={MODELS}
        modelsLoading={false}
        modelsError={null}
        selectedGenerationModelName={MODEL_ID}
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
    expect(html).not.toContain(MODEL_ID);
  });

  test("keeps the created Agent runtime model read-only in the blueprint", () => {
    const html = renderAgentUi(
      <AgentBuilderBlueprintReview
        blueprint={blueprint()}
        agentName="Code Review"
        agentSlug="code-review"
        agentSlugError={null}
        models={MODELS}
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
        onAgentNameChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );

    expect(html).toContain("GPT-5.6 Luna");
    expect(html).toContain('value="Code Review"');
    expect(html).toContain("code-review");
    const inputId = html.indexOf('id="agent-builder-commit-name"');
    const inputStart = html.lastIndexOf("<input", inputId);
    expect(inputStart).toBeGreaterThanOrEqual(0);
    const nameInput = html.slice(inputStart, html.indexOf(">", inputStart));
    expect(nameInput).not.toMatch(/\sdisabled(?:=|\s|\/|>)/u);
    expect(html).not.toContain(">default<");
    expect(html).not.toContain(MODEL_ID);
    expect(html).not.toContain('aria-label="选择 Agent 模型"');
  });

  test("blocks creation and explains recovery when the blueprint model is unavailable", () => {
    const html = renderAgentUi(
      <AgentBuilderBlueprintReview
        blueprint={{ ...blueprint(), model_ref: "retired-model" }}
        agentName="Code Review"
        agentSlug="code-review"
        agentSlugError={null}
        models={MODELS}
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
        onAgentNameChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );
    const labelIndex = html.lastIndexOf("创建 Agent 草稿");
    const buttonStart = html.lastIndexOf("<button", labelIndex);
    const buttonEnd = html.indexOf(">", buttonStart);

    expect(html).toContain("Agent 模型不可用");
    expect(html).toContain("请继续对话，让 Agent 改用当前可用模型");
    expect(html.slice(buttonStart, buttonEnd)).toContain("disabled");
  });

  test("shows conflict fields and blocks creation until AI regeneration clears errors", () => {
    const html = renderAgentUi(
      <AgentBuilderBlueprintReview
        blueprint={blueprint()}
        agentName="Code Review"
        agentSlug="code-review"
        agentSlugError={null}
        models={MODELS}
        assumptions={["仅审查当前项目代码"]}
        conflicts={[
          {
            code: "IDENTITY_SCOPE",
            fields: ["soul", "identity"],
            message: "人格设定与身份定位冲突",
            severity: "error",
          },
        ]}
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
        onAgentNameChange={() => undefined}
        onEdit={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onCreate={() => undefined}
      />,
    );
    const labelIndex = html.lastIndexOf("创建 Agent 草稿");
    const buttonStart = html.lastIndexOf("<button", labelIndex);
    const buttonEnd = html.indexOf(">", buttonStart);

    expect(html).toContain("仅审查当前项目代码");
    expect(html).toContain("人格设定与身份定位冲突");
    expect(html).toContain("SOUL.md");
    expect(html).toContain("IDENTITY.md");
    expect(html).toContain("请继续对话，让 Agent 重新生成设计稿");
    expect(html.slice(buttonStart, buttonEnd)).toContain("disabled");
  });

  test("releases stale commands and reloads the session after a Builder conflict", async () => {
    const release = rs.fn();
    const refetch = rs.fn(async () => undefined);

    await expect(
      recoverAgentBuilderConflict(
        new AgentBuilderApiError(
          409,
          "AGENT_BUILDER_CONFLICT",
          "stale revision",
        ),
        release,
        refetch,
      ),
    ).resolves.toBe(true);
    expect(release).toHaveBeenCalledTimes(1);
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  test("does not misclassify a domain 409 as a revision conflict", async () => {
    const release = rs.fn();
    const refetch = rs.fn(async () => undefined);

    await expect(
      recoverAgentBuilderConflict(
        new AgentBuilderApiError(
          409,
          "AGENT_DESIGN_CONFLICT_UNRESOLVED",
          "unresolved conflict",
        ),
        release,
        refetch,
      ),
    ).resolves.toBe(false);
    expect(release).not.toHaveBeenCalled();
    expect(refetch).not.toHaveBeenCalled();
  });

  test("rebases the server baseline without overwriting a local blueprint draft", () => {
    const localDraft = { ...blueprint(), description: "本地尚未保存的修改" };
    const serverBlueprint = { ...blueprint(), description: "其他页面的修改" };

    expect(
      rebaseAgentBuilderBlueprint(localDraft, serverBlueprint, true),
    ).toEqual({ baseline: serverBlueprint, draft: localDraft });
  });

  test("keeps a locally renamed Agent when a conflict refetches the session", () => {
    expect(
      rebaseAgentBuilderName("Available Agent", "duplicate-agent", true),
    ).toEqual({ baseline: "duplicate-agent", draft: "Available Agent" });
  });

  test("lets readers resume a session without exposing the cancel mutation", () => {
    const summary = {
      id: session().id,
      slug: session().slug,
      display_name: session().display_name,
      status: "generating",
      revision: 2,
      updated_at: TIMESTAMP,
    } satisfies AgentBuilderSessionSummary;
    const html = renderAgentUi(
      <AgentBuilderResumeBannerView
        projectSlug="alpha"
        sessions={[summary]}
        canAuthor={false}
        onDelete={async () => undefined}
      />,
    );

    expect(html).toContain(`/projects/alpha/agents/new/${summary.id}`);
    expect(html).not.toContain(`aria-label="删除 ${summary.display_name}"`);
  });

  test("uses the refreshed resume-list revision after a cancel CAS conflict", () => {
    const selected = {
      id: session().id,
      slug: session().slug,
      display_name: session().display_name,
      status: "generating",
      revision: 2,
      updated_at: TIMESTAMP,
    } satisfies AgentBuilderSessionSummary;
    const refreshed = {
      ...selected,
      status: "proposal_ready",
      revision: 4,
    } satisfies AgentBuilderSessionSummary;

    expect(resolveAgentBuilderDeleteTarget(selected, [refreshed])).toBe(
      refreshed,
    );
    expect(resolveAgentBuilderDeleteTarget(selected, [])).toBeNull();
  });
});
