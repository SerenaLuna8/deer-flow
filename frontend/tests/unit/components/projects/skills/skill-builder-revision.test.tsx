import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectSkillDetailActions,
  skillPublishStaleBaseMessage,
} from "@/components/projects/assets/project-asset-detail-sheet";
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
  test("shows the entry only for live project skills with a published version", () => {
    expect(
      skillRevisionEntryVisible(["shared_assets.edit"], {
        scope: "project",
        status: "active",
        current_published_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(true);
    expect(
      skillRevisionEntryVisible(["shared_assets.edit"], {
        scope: "system",
        status: "active",
        current_published_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(false);
    expect(
      skillRevisionEntryVisible(["shared_assets.edit"], {
        scope: "project",
        status: "archived",
        current_published_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(false);
    expect(
      skillRevisionEntryVisible(["shared_assets.read"], {
        scope: "project",
        status: "active",
        current_published_version_id: "55555555-5555-4555-8555-555555555555",
      }),
    ).toBe(false);
  });
});

describe("ProjectSkillDetailActions", () => {
  test("places the AI revision action after the existing top actions", () => {
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
        additionalActions={<button type="button">AI修改</button>}
      />,
    );

    expect(zhCN.skills.builder.revision.button).toBe("AI修改");
    expect(html.indexOf("创建新版本")).toBeLessThan(html.indexOf("删除 Skill"));
    expect(html.indexOf("删除 Skill")).toBeLessThan(html.indexOf("AI修改"));
    expect(html).not.toContain("以当前发布版本为基线");
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
    ).toContain("较旧的基线");
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
    expect(revising).toContain("已加载");
    expect(revising).toContain("catalog-auditor");
    expect(revising).toContain("v3");

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
    expect(html).toContain("Loaded");
    expect(html).toContain("catalog-auditor");
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

  test("does not repeat processing while an active Run status is visible", () => {
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

    expect(activeRun).toContain("正在执行");
    expect(activeRun).not.toContain("Builder Agent 正在处理");

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
});

describe("SkillBuilderRevisionCommitSuccess", () => {
  test("names the created draft version and links to publish", () => {
    expect(
      skillBuilderRevisionCommitSuccessCopy(4, zhCN.skills.builder.success),
    ).toBe("已创建草稿版本 v4，前往发布");
    expect(
      skillBuilderRevisionCommitSuccessCopy(4, enUS.skills.builder.success),
    ).toBe("Created draft version v4. Go publish it");
    const html = renderUi(
      <SkillBuilderRevisionCommitSuccess
        versionNumber={4}
        href="/projects/demo/skills?skill_id=55555555-5555-4555-8555-555555555555&skill_version_id=66666666-6666-4666-8666-666666666666"
      />,
    );
    expect(html).toContain("已创建草稿版本 v4，前往发布");
    expect(html).toContain("前往发布");
    expect(html).toContain(
      "/projects/demo/skills?skill_id=55555555-5555-4555-8555-555555555555&amp;skill_version_id=66666666-6666-4666-8666-666666666666",
    );
  });
});

describe("skillPublishStaleBaseMessage", () => {
  test("names the live version and the draft base", () => {
    expect(
      skillPublishStaleBaseMessage(
        {
          liveVersionNumber: 5,
          baseVersionNumber: 3,
        },
        zhCN.skills.builder.publish,
      ),
    ).toBe(
      "当前线上已是 v5，本次将以基于 v3 的版本覆盖。确认发布后，它将替换线上正在使用的版本，之后仍可在版本历史中回退。",
    );
  });
});
