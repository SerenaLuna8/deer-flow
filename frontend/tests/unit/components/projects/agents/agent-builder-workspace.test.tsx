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
  type AgentBuilderActivity,
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
    skill_refs: [],
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
    generation_preference: null,
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
        selectedGenerationMode="pro"
        canAuthor
        mutationPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
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
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html).toContain("请重点检查并发安全");
    expect(html).toContain("正在设计 Agent");
  });

  test("renders user, real process, then AI result and collapses a terminal turn", () => {
    const operationId = "44444444-4444-4444-8444-444444444444";
    const completed = {
      ...session(),
      status: "proposal_ready" as const,
      messages: [
        {
          id: "user-1",
          role: "user" as const,
          content: "请设计审查助手",
          operation_id: operationId,
          created_at: TIMESTAMP,
        },
        {
          id: "assistant-1",
          role: "assistant" as const,
          content: "候选已经就绪",
          operation_id: operationId,
          created_at: TIMESTAMP,
        },
      ],
    };
    const activities: AgentBuilderActivity[] = [
      {
        seq: "9007199254740993",
        operation_id: operationId,
        kind: "turn_accepted",
        attempt: null,
        payload: {},
        created_at: TIMESTAMP,
      },
      {
        seq: "9007199254740994",
        operation_id: operationId,
        kind: "reasoning",
        attempt: 1,
        payload: { text: "首次真实思考" },
        created_at: TIMESTAMP,
      },
      {
        seq: "9007199254740995",
        operation_id: operationId,
        kind: "reasoning",
        attempt: 2,
        payload: { text: "修复真实思考" },
        created_at: TIMESTAMP,
      },
      {
        seq: "9007199254740996",
        operation_id: operationId,
        kind: "turn_terminal",
        attempt: null,
        payload: { status: "completed", duration_ms: 1234 },
        created_at: TIMESTAMP,
      },
    ];

    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={completed}
        activities={activities}
        composerText=""
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
        canAuthor
        mutationPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html.indexOf("请设计审查助手")).toBeLessThan(
      html.indexOf("思考与执行过程"),
    );
    expect(html.indexOf("思考与执行过程")).toBeLessThan(
      html.indexOf("候选已经就绪"),
    );
    expect(html).toContain("首次真实思考");
    expect(html).toContain("修复真实思考");
    expect(html).not.toContain("已接收请求");
    expect(html).not.toContain("本轮生成结束");
    expect(html).not.toMatch(/<details[^>]* open/);
    const activityBlock = /<details[^>]*>/.exec(html)?.[0];
    expect(activityBlock).toContain("ml-0");
    expect(activityBlock).not.toMatch(/sm:ml-/);
  });

  test("renders each Agent document once without duplicate progress copy", () => {
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={{
          ...session(),
          status: "generating",
          progress: [
            {
              id: "agents_instructions",
              label: "AGENTS.md",
              status: "running",
            },
            { id: "soul", label: "SOUL.md", status: "pending" },
            { id: "identity", label: "IDENTITY.md", status: "pending" },
            { id: "user_context", label: "USER.md", status: "pending" },
          ],
        }}
        composerText=""
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
        canAuthor
        mutationPending
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    for (const name of ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md"]) {
      expect(
        html.match(new RegExp(name.replace(".", "\\."), "g")),
      ).toHaveLength(1);
    }
  });

  test("keeps an active process open without inventing reasoning", () => {
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={{ ...session(), status: "generating" }}
        activities={[
          {
            seq: "1",
            operation_id: "44444444-4444-4444-8444-444444444444",
            kind: "turn_accepted",
            attempt: null,
            payload: {},
            created_at: TIMESTAMP,
          },
        ]}
        composerText=""
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
        canAuthor
        mutationPending
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html).toMatch(/<details[^>]* open=""/);
    expect(html).not.toContain("首次真实思考");
    expect(html).toContain("停止本轮生成");
  });

  test("keeps Stop this generation enabled while the generation request is pending", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AgentBuilderConversationView
          session={{ ...session(), status: "generating" }}
          composerText=""
          models={MODELS}
          selectedGenerationModelName={MODEL_ID}
          selectedGenerationMode="pro"
          canAuthor
          mutationPending
          stopPending={false}
          blueprintEditing={false}
          blueprintDraft={null}
          blueprintDirty={false}
          errorMessage={null}
          onComposerTextChange={() => undefined}
          onSubmitMessage={() => undefined}
          onStopGeneration={() => undefined}
          onSubmitClarification={() => undefined}
        />
      </I18nProvider>,
    );
    const labelIndex = html.indexOf('aria-label="Stop this generation"');
    const composerStart = html.lastIndexOf(
      "data-agent-builder-composer-shell",
      labelIndex,
    );
    const buttonStart = html.lastIndexOf("<button", labelIndex);
    const buttonEnd = html.indexOf(">", labelIndex);
    const buttonMarkup = html.slice(
      buttonStart,
      html.indexOf("</button>", buttonStart) + "</button>".length,
    );

    expect(labelIndex).toBeGreaterThanOrEqual(0);
    expect(html.slice(buttonStart, buttonEnd)).not.toMatch(
      /\sdisabled(?:=|\s|\/|>)/u,
    );
    expect(html.slice(composerStart, buttonEnd)).not.toContain(
      'aria-disabled="true"',
    );
    expect(buttonMarkup).toContain("fill-current");
    expect(buttonMarkup).not.toContain("animate-spin");
  });

  test("does not offer generation stop while the final Agent commit is running", () => {
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={{ ...session(), status: "committing" }}
        composerText=""
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
        canAuthor
        mutationPending
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html).toContain("正在创建 Agent");
    expect(html).not.toContain("停止本轮生成");
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
        selectedGenerationMode="pro"
        canAuthor
        mutationPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onModelsRetry={() => undefined}
        onGenerationModelChange={() => undefined}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html).toContain('aria-label="选择创建 Agent 的对话模型"');
    expect(html).toContain("GPT-5.6 Luna");
    expect(html).toContain("Pro");
    expect(html).not.toContain(MODEL_ID);
  });

  test("uses the ordinary chat composer hierarchy without changing Builder controls", () => {
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={session()}
        composerText="补充审查范围"
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
        selectedGenerationMode="pro"
        canAuthor
        mutationPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onGenerationModelChange={() => undefined}
        onGenerationModeChange={() => undefined}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );
    const shellStart = html.indexOf("data-agent-builder-composer-shell");
    const shellTagEnd = html.indexOf(">", shellStart);
    const composerStart = html.indexOf(
      "data-agent-builder-composer=",
      shellStart,
    );
    const composerEnd = html.indexOf("</form>", composerStart);
    const composerMarkup = html.slice(composerStart, composerEnd);
    const leadingStart = composerMarkup.indexOf(
      "data-agent-builder-composer-leading=",
    );
    const trailingStart = composerMarkup.indexOf(
      "data-agent-builder-composer-trailing=",
    );
    const sendLabel = 'aria-label="发送"';
    const sendLabelIndex = composerMarkup.indexOf(sendLabel);
    const sendButtonStart = composerMarkup.lastIndexOf(
      "<button",
      sendLabelIndex,
    );
    const sendButtonEnd = composerMarkup.indexOf("</button>", sendLabelIndex);
    const sendButtonMarkup = composerMarkup.slice(
      sendButtonStart,
      sendButtonEnd,
    );

    expect(shellStart).toBeGreaterThanOrEqual(0);
    expect(html.slice(shellStart, shellTagEnd)).not.toContain("border-t");
    expect(html.slice(shellStart, shellTagEnd)).toContain("pb-4");
    expect(composerStart).toBeGreaterThanOrEqual(0);
    expect(composerMarkup).toContain("backdrop-blur-sm");
    expect(leadingStart).toBeGreaterThanOrEqual(0);
    expect(trailingStart).toBeGreaterThan(leadingStart);
    expect(composerMarkup.slice(leadingStart, trailingStart)).toContain("Pro");
    expect(composerMarkup.slice(leadingStart, trailingStart)).not.toContain(
      "GPT-5.6 Luna",
    );
    expect(composerMarkup.slice(trailingStart)).toContain("GPT-5.6 Luna");
    expect(sendButtonMarkup).toContain("rounded-full");
    expect(sendButtonMarkup).toContain("lucide-arrow-up");
  });

  test("locks the model picker from another page while generation is active", () => {
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={{ ...session(), status: "generating" }}
        composerText=""
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
        selectedGenerationMode="pro"
        canAuthor
        mutationPending={false}
        blueprintEditing={false}
        blueprintDraft={null}
        blueprintDirty={false}
        errorMessage={null}
        onGenerationModelChange={() => undefined}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );
    const labelIndex = html.indexOf('aria-label="选择创建 Agent 的对话模型"');
    const buttonStart = html.lastIndexOf("<button", labelIndex);
    const buttonEnd = html.indexOf(">", labelIndex);

    expect(labelIndex).toBeGreaterThanOrEqual(0);
    expect(html.slice(buttonStart, buttonEnd)).toContain("disabled");
  });

  test("keeps the completed blueprint out of the conversation and links to its panel", () => {
    const html = renderAgentUi(
      <AgentBuilderConversationView
        session={{
          ...session(),
          status: "completed",
          blueprint: blueprint(),
          blueprint_checksum: "a".repeat(64),
          created_agent_id: "44444444-4444-4444-8444-444444444444",
        }}
        composerText=""
        models={MODELS}
        selectedGenerationModelName={MODEL_ID}
        selectedGenerationMode="pro"
        canAuthor={false}
        mutationPending={false}
        blueprintEditing={false}
        blueprintDraft={blueprint()}
        blueprintDirty={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
        onOpenBlueprint={() => undefined}
      />,
    );

    expect(html).toContain('data-testid="agent-builder-blueprint-summary"');
    expect(html).toContain("Agent 设计稿已就绪");
    expect(html).toContain("查看设计稿");
    expect(html).not.toContain('id="agent-builder-commit-name"');
    expect(html).not.toContain('aria-label="Agent 指令文档"');
    expect(html).not.toContain("运行配置");
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
    expect(html).not.toContain("生成结果");
    expect(html).not.toContain("请检查模型生成的设置和 Agent 名称");
    expect(html).not.toContain("显示名称与 slug 均使用此值");
    expect(html).not.toContain("查看并编辑四个固定 Markdown 文档");
    expect(html).not.toContain(">固定文件<");
    expect(html).toContain("创建初始 Agent Definition");
    expect(html).toContain("创建后默认停用，需手动启用");
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
    const labelIndex = html.lastIndexOf("创建 Agent");
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
    const labelIndex = html.lastIndexOf("创建 Agent");
    const buttonStart = html.lastIndexOf("<button", labelIndex);
    const buttonEnd = html.indexOf(">", buttonStart);

    expect(html).toContain("仅审查当前项目代码");
    expect(html).toContain("人格设定与身份定位冲突");
    expect(html).toContain("SOUL.md");
    expect(html).toContain("IDENTITY.md");
    expect(html).toContain('aria-label="在文档中查看 SOUL.md"');
    expect(html).toContain('aria-label="在文档中查看 IDENTITY.md"');
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
