import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectSkillDetailActions } from "@/components/projects/assets/project-asset-detail-sheet";
import { SkillBuilderStartView } from "@/components/projects/skills/skill-builder-start";
import {
  SkillBuilderConversationView,
  SkillBuilderRevisionCommitSuccess,
  skillBuilderRevisionCommitSuccessCopy,
  skillBuilderWorkspaceErrorMessage,
} from "@/components/projects/skills/skill-builder-workspace";
import {
  skillRevisionEntryErrorMessage,
  skillRevisionEntryVisible,
} from "@/components/projects/skills/skill-revision-entry";
import { enUS, zhCN } from "@/core/i18n";
import { I18nProvider } from "@/core/i18n/context";
import {
  SkillBuilderApiError,
  type SkillBuilderSession,
} from "@/core/skill-builder";

const NOW = "2026-08-13T08:00:00+08:00";
const errors = zhCN.skills.builder.errors;

function renderUi(node: React.ReactNode, locale: "zh-CN" | "en-US" = "zh-CN") {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>{node}</I18nProvider>,
  );
}

function session(
  overrides: Partial<SkillBuilderSession> = {},
): SkillBuilderSession {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    project_id: "22222222-2222-4222-8222-222222222222",
    owner_user_id: "owner-1",
    thread_id: "33333333-3333-4333-8333-333333333333",
    slug: "catalog-auditor",
    display_name: "Catalog auditor",
    status: "draft_ready",
    revision: 1,
    messages: [],
    active_clarification: null,
    progress: [],
    files: [],
    draft_checksum: "b".repeat(64),
    validation: null,
    error_code: null,
    error_message: null,
    created_skill_id: null,
    session_kind: "revise",
    target_skill_id: "55555555-5555-4555-8555-555555555555",
    base_version_id: "66666666-6666-4666-8666-666666666666",
    base_version_number: 3,
    base_payload_checksum: "b".repeat(64),
    target_skill_deleted: false,
    base_files: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

describe("skillRevisionEntryVisible", () => {
  test("shows the entry only for live project skills with a Current Version", () => {
    expect(
      skillRevisionEntryVisible(["shared_assets.edit"], {
        scope: "project",
        status: "active",
        current_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(true);
    expect(
      skillRevisionEntryVisible(["shared_assets.edit"], {
        scope: "system",
        status: "active",
        current_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(false);
    expect(
      skillRevisionEntryVisible(["shared_assets.edit"], {
        scope: "project",
        status: "archived",
        current_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(false);
    expect(
      skillRevisionEntryVisible(["shared_assets.read"], {
        scope: "project",
        status: "active",
        current_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(false);
  });
});

describe("ProjectSkillDetailActions", () => {
  test("places activation beside create before AI revision and delete", () => {
    const html = renderUi(
      <ProjectSkillDetailActions
        actionPending={false}
        canAuthor
        canDelete
        editing={false}
        hasSelectedVersion
        versionDirty={false}
        versionSelectionPending={false}
        onCreateVersion={() => undefined}
        onDelete={() => undefined}
        primaryVersionAction={<button type="button">激活版本</button>}
        additionalActions={<button type="button">AI修改</button>}
      />,
    );

    expect(zhCN.skills.builder.revision.button).toBe("AI修改");
    expect(html.indexOf("创建新版本")).toBeLessThan(html.indexOf("激活版本"));
    expect(html.indexOf("激活版本")).toBeLessThan(html.indexOf("AI修改"));
    expect(html.indexOf("AI修改")).toBeLessThan(html.indexOf("删除 Skill"));
    expect(html).not.toContain("从历史版本恢复");
  });
});

describe("skillRevisionEntryErrorMessage", () => {
  test("explains a live revision session conflict", () => {
    expect(
      skillRevisionEntryErrorMessage(
        new SkillBuilderApiError(
          409,
          "SKILL_BUILDER_CONFLICT",
          "exists",
          "SKILL_DESIGN_TARGET_SESSION_EXISTS",
        ),
        errors,
      ),
    ).toContain("未完成的修订会话");
  });
});

describe("skillBuilderWorkspaceErrorMessage", () => {
  test("maps revision-specific Gateway codes", () => {
    expect(
      skillBuilderWorkspaceErrorMessage(
        new SkillBuilderApiError(
          409,
          "SKILL_BUILDER_CONFLICT",
          "deleted",
          "SKILL_DESIGN_TARGET_DELETED",
        ),
        errors,
      ),
    ).toContain("目标 Skill 已被删除");
    expect(
      skillBuilderWorkspaceErrorMessage(
        new SkillBuilderApiError(
          409,
          "SKILL_BUILDER_CONFLICT",
          "noop",
          "SKILL_DESIGN_NO_CHANGES",
        ),
        errors,
      ),
    ).toContain("完全一致");
    expect(
      skillBuilderWorkspaceErrorMessage(
        new SkillBuilderApiError(
          409,
          "SKILL_BUILDER_CONFLICT",
          "stale",
          "SKILL_DESIGN_BASE_STALE",
        ),
        errors,
      ),
    ).toContain("Current Version 已变化");
  });
});

describe("SkillBuilderConversationView", () => {
  test("shows the revision empty-state and target-deleted terminal banner", () => {
    const revising = renderUi(
      <SkillBuilderConversationView
        session={session()}
        composerText=""
        canAuthor
        dirty={false}
        pending={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );
    expect(revising).not.toContain("已加载");
    expect(revising).not.toContain("catalog-auditor");
    expect(revising).toContain('aria-label="描述要修改的内容"');

    const deleted = renderUi(
      <SkillBuilderConversationView
        session={session({
          target_skill_deleted: true,
          status: "failed",
          error_code: "SKILL_DESIGN_TARGET_DELETED",
          error_message: "The revision target Skill was deleted",
        })}
        composerText=""
        canAuthor
        dirty={false}
        pending={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );
    expect(deleted).toContain("目标 Skill 已被删除");
    expect(deleted).not.toContain('aria-label="描述要修改的内容"');
    expect(deleted).not.toContain("已加载");
  });

  test("renders English revision copy when the locale is en-US", () => {
    const html = renderUi(
      <SkillBuilderConversationView
        session={session()}
        composerText=""
        canAuthor
        dirty={false}
        pending={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
      "en-US",
    );
    expect(html).not.toContain("Loaded");
    expect(html).not.toContain("catalog-auditor");
    expect(html).toContain('aria-label="Describe what to change"');
  });

  test("does not repeat a terminal error already stored as the last assistant message", () => {
    const failure = "Skill 生成暂时不可用，请稍后重试。";
    const html = renderUi(
      <SkillBuilderConversationView
        session={session({
          session_kind: "create",
          status: "failed",
          messages: [
            {
              id: "failure-message",
              role: "assistant",
              content: failure,
              created_at: NOW,
            },
          ],
          error_code: "AGENT_EXECUTION_FAILED",
          error_message: failure,
        })}
        composerText=""
        canAuthor
        dirty={false}
        pending={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html.split(failure)).toHaveLength(2);
  });

  test("uses a generic status until the isolated Activity stream arrives", () => {
    const activeRun = renderUi(
      <SkillBuilderConversationView
        session={session({
          status: "generating",
          activeRun: {
            runId: "77777777-7777-4777-8777-777777777777",
            status: "running",
            streamUrl: "/api/runs/active/stream",
          },
        })}
        composerText=""
        canAuthor
        dirty={false}
        pending={false}
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(activeRun).toContain("Builder Agent 正在处理");
    expect(activeRun).not.toContain("正在执行");

    const beforeAdmission = renderUi(
      <SkillBuilderConversationView
        session={session()}
        composerText=""
        pendingUserMessage="继续生成候选文件"
        canAuthor
        dirty={false}
        pending
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(beforeAdmission).toContain("Builder Agent 正在处理");
  });

  test("renders each operation as user message, process, then assistant result", () => {
    const operationId = "88888888-8888-4888-8888-888888888888";
    const html = renderUi(
      <SkillBuilderConversationView
        session={session({
          messages: [
            {
              id: "user-turn",
              role: "user",
              content: "生成一个巡检 Skill",
              created_at: NOW,
              operation_id: operationId,
            },
            {
              id: "assistant-turn",
              role: "assistant",
              content: "候选文件已准备好",
              created_at: NOW,
              operation_id: operationId,
            },
          ],
        })}
        composerText=""
        canAuthor
        dirty={false}
        pending={false}
        errorMessage={null}
        activities={[
          {
            seq: "1",
            operation_id: operationId,
            run_id: "99999999-9999-4999-8999-999999999999",
            attempt: null,
            kind: "request_accepted",
            payload: {},
            created_at: NOW,
          },
          {
            seq: "2",
            operation_id: operationId,
            run_id: "99999999-9999-4999-8999-999999999999",
            attempt: 1,
            kind: "reasoning",
            payload: { text: "先确认输入和输出。" },
            created_at: NOW,
          },
          {
            seq: "3",
            operation_id: operationId,
            run_id: "99999999-9999-4999-8999-999999999999",
            attempt: 1,
            kind: "tool_started",
            payload: {
              tool_call_id: "tool-1",
              tool_name: "read_candidate_file",
            },
            created_at: NOW,
          },
          {
            seq: "4",
            operation_id: operationId,
            run_id: "99999999-9999-4999-8999-999999999999",
            attempt: 1,
            kind: "reasoning",
            payload: { text: "然后生成候选文件。" },
            created_at: NOW,
          },
          {
            seq: "5",
            operation_id: operationId,
            run_id: "99999999-9999-4999-8999-999999999999",
            attempt: 1,
            kind: "run_terminal",
            payload: { status: "completed" },
            created_at: NOW,
          },
        ]}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html.indexOf("生成一个巡检 Skill")).toBeLessThan(
      html.indexOf("思考与执行过程"),
    );
    expect(html.indexOf("思考与执行过程")).toBeLessThan(
      html.indexOf("候选文件已准备好"),
    );
    expect(html).toContain("先确认输入和输出。");
    expect(html).not.toContain("已接收请求");
    expect(html).not.toContain("本次操作结束");
    expect(html.indexOf("先确认输入和输出。")).toBeLessThan(
      html.indexOf("read_candidate_file"),
    );
    expect(html.indexOf("read_candidate_file")).toBeLessThan(
      html.indexOf("然后生成候选文件。"),
    );
  });

  test("omits optional start, intro, and progress explanations", () => {
    const start = renderUi(
      <SkillBuilderStartView
        name="catalog-auditor"
        normalizedName="catalog-auditor"
        errorMessage={null}
        pending={false}
        onNameChange={() => undefined}
        onSubmit={() => undefined}
      />,
    );
    const conversation = renderUi(
      <SkillBuilderConversationView
        session={session({
          messages: [],
          status: "generating",
          progress: [
            { id: "requirements", label: "确认需求", status: "completed" },
            { id: "candidate", label: "生成候选文件", status: "running" },
          ],
        })}
        composerText=""
        canAuthor
        dirty={false}
        pending
        errorMessage={null}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(start).not.toContain("frontmatter 中不可变的 name");
    expect(conversation).not.toContain("创建进度");
    expect(conversation).not.toContain("新 Skill 的名称是");
    expect(conversation).toContain("确认需求");
    expect(conversation).toContain("生成候选文件");
  });

  test("keeps the completed Builder readable with a target-version link", () => {
    const html = renderUi(
      <SkillBuilderConversationView
        session={session({ status: "completed" })}
        composerText=""
        canAuthor
        dirty={false}
        pending={false}
        errorMessage={null}
        completion={{
          message: "Skill 已创建并默认停用，可前往查看并激活。",
          skillHref: "/projects/example/skills?skill_id=1",
          versionHref: "/projects/example/skills?skill_id=1&skill_version_id=2",
          secretHref: null,
        }}
        onComposerTextChange={() => undefined}
        onSubmitMessage={() => undefined}
        onSubmitClarification={() => undefined}
      />,
    );

    expect(html).toContain("Skill 已创建并默认停用");
    expect(html).toContain('href="/projects/example/skills?skill_id=1"');
    expect(html).toContain("查看 Skill");
    expect(html).toContain("查看候选版本");
    expect(html).not.toContain("<textarea");
  });
});

describe("SkillBuilderRevisionCommitSuccess", () => {
  test("names the saved Candidate Version and links to activation", () => {
    expect(
      skillBuilderRevisionCommitSuccessCopy(4, zhCN.skills.builder.success),
    ).toBe("已保存候选版本 v4，前往激活");
    expect(
      skillBuilderRevisionCommitSuccessCopy(4, enUS.skills.builder.success),
    ).toBe("Saved Candidate Version v4. Go activate it");
    const html = renderUi(
      <SkillBuilderRevisionCommitSuccess
        versionNumber={4}
        href="/projects/demo/skills?skill_id=55555555-5555-4555-8555-555555555555&skill_version_id=66666666-6666-4666-8666-666666666666"
      />,
    );
    expect(html).toContain("已保存候选版本 v4，前往激活");
    expect(html).toContain("前往激活");
    expect(html).toContain(
      "/projects/demo/skills?skill_id=55555555-5555-4555-8555-555555555555&amp;skill_version_id=66666666-6666-4666-8666-666666666666",
    );
  });
});
