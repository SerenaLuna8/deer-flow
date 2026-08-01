import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillBuilderCandidateWorkbench } from "@/components/projects/skills/skill-builder-candidate-workbench";
import { SkillBuilderResumeBanner } from "@/components/projects/skills/skill-builder-resume-banner";
import {
  SkillBuilderStartView,
  skillBuilderErrorMessage,
} from "@/components/projects/skills/skill-builder-start";
import {
  SkillBuilderConversationView,
  skillBuilderDraftChanges,
} from "@/components/projects/skills/skill-builder-workspace";
import { I18nProvider } from "@/core/i18n/context";
import {
  SkillBuilderApiError,
  skillBuilderSessionPath,
  type SkillBuilderSession,
} from "@/core/skill-builder";

const checksum = "c".repeat(64);
const session: SkillBuilderSession = {
  id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  owner_user_id: "user-1",
  thread_id: "33333333-3333-4333-8333-333333333333",
  slug: "paper-review",
  display_name: "paper-review",
  status: "validated",
  revision: 4,
  messages: [
    {
      id: "message-1",
      role: "user",
      content: "创建一个论文审阅 Skill。",
      created_at: "2026-07-27T08:00:00Z",
    },
    {
      id: "message-2",
      role: "assistant",
      content: "候选文件已经生成，你可以检查和编辑。",
      created_at: "2026-07-27T08:00:01Z",
    },
  ],
  active_clarification: null,
  progress: [
    { id: "interview", label: "确认用途与触发条件", status: "completed" },
    { id: "draft", label: "生成候选文件", status: "completed" },
  ],
  files: [
    {
      path: "SKILL.md",
      media_type: "text/markdown",
      size_bytes: 42,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      content: "---\nname: paper-review\n---\n\n# Paper review",
    },
    {
      path: "references/guide.md",
      media_type: "text/markdown",
      size_bytes: 7,
      sha256: "b".repeat(64),
      encoding: "utf-8",
      content: "# Guide",
    },
  ],
  draft_checksum: checksum,
  validation: {
    draft_checksum: checksum,
    validated_at: "2026-07-27T08:01:00Z",
    description: "Review academic papers",
    frontmatter: { name: "paper-review" },
    compatibility: ">=2.1",
    secret_requirements: [{ name: "SEARCH_TOKEN", optional: true }],
    scan_decision: "warn",
    scan_rule_ids: ["external-network"],
    scan_summary: { warnings: 1 },
  },
  error_code: null,
  error_message: null,
  created_skill_id: null,
  created_at: "2026-07-27T07:58:00Z",
  updated_at: "2026-07-27T08:01:00Z",
};

describe("Skill Builder UI", () => {
  test("renders the one-field Skill naming step", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderStartView
        name="Paper Review"
        normalizedName="paper-review"
        errorMessage={null}
        pending={false}
        onNameChange={() => undefined}
        onSubmit={() => undefined}
      />,
    );
    expect(html).toContain("给新 Skill 起个名字");
    expect(html).toContain("paper-review");
    expect(html).toContain("继续");
  });

  test("shows only unfinished Builder sessions in a recovery banner", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderResumeBanner
        projectSlug="default project"
        sessions={[
          {
            id: session.id,
            slug: session.slug,
            display_name: session.display_name,
            status: "draft_ready",
            updated_at: session.updated_at,
          },
          {
            id: "99999999-9999-4999-8999-999999999999",
            slug: "done",
            display_name: "done",
            status: "completed",
            updated_at: session.updated_at,
          },
        ]}
      />,
    );
    expect(html).toContain("继续创建未完成的 Skill");
    expect(html).toContain(
      `/projects/default%20project/skills/new/${session.id}`,
    );
    expect(html).not.toContain(">done<");
  });

  test("renders conversation and clarification without exposing internal prompts", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <SkillBuilderConversationView
          session={{
            ...session,
            status: "awaiting_clarification",
            active_clarification: {
              version: 1,
              kind: "human_input_request",
              source: "skill_builder",
              request_id: "clarification-1",
              clarification_type: "skill_design",
              title: "补充 Skill 设计信息",
              question: "这个 Skill 在什么情况下触发？",
              context: "用于明确触发条件。",
              input_mode: "free_text",
              options: [],
            },
          }}
          composerText=""
          canAuthor
          dirty={false}
          pending={false}
          errorMessage={null}
          onComposerTextChange={() => undefined}
          onSubmitMessage={() => undefined}
          onSubmitClarification={() => undefined}
        />
      </I18nProvider>,
    );
    expect(html).toContain("创建一个论文审阅 Skill");
    expect(html).toContain("这个 Skill 在什么情况下触发");
    expect(html).toContain("等待你回答上方问题");
  });

  test("renders a pending user turn before the Builder response completes", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <SkillBuilderConversationView
          session={{
            ...session,
            status: "interviewing",
            messages: [],
            files: [],
            draft_checksum: null,
            validation: null,
          }}
          composerText=""
          pendingUserMessage="写代码"
          canAuthor
          dirty={false}
          pending
          errorMessage={null}
          onComposerTextChange={() => undefined}
          onSubmitMessage={() => undefined}
          onSubmitClarification={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain("写代码");
    expect(html).toContain("skill-creator 正在生成候选文件");
  });

  test("renders one selected candidate file, validation warning and disabled-by-default commit", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderCandidateWorkbench
        files={session.files}
        selectedPath="SKILL.md"
        draftContent={session.files[0]?.content ?? ""}
        displayMode="preview"
        canAuthor
        readOnly={false}
        dirty={false}
        baselineStale={false}
        pending={false}
        validation={session.validation}
        canValidate
        canCommit
        acknowledgeWarnings={false}
        errorMessage={null}
        onSelectPath={() => undefined}
        onDraftContentChange={() => undefined}
        onDisplayModeChange={() => undefined}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onValidate={() => undefined}
        onAcknowledgeWarningsChange={() => undefined}
        onCommit={() => undefined}
      />,
    );

    expect(html).toContain("候选文件包");
    expect(html).toContain("SKILL.md");
    expect(html).toContain("references");
    expect(html).toContain("external-network");
    expect(html).toContain("确认并接受上述警告");
    expect(html).toContain("创建 Skill（默认停用）");
    expect(html).toContain('disabled=""');
  });

  test("encodes the Builder route and maps safe failures", () => {
    expect(skillBuilderSessionPath("default project", session.id)).toBe(
      `/projects/default%20project/skills/new/${session.id}`,
    );
    expect(
      skillBuilderErrorMessage(
        new SkillBuilderApiError(409, "SKILL_BUILDER_CONFLICT", "conflict"),
      ),
    ).toContain("已存在同名 Skill");
    expect(
      skillBuilderErrorMessage(
        new SkillBuilderApiError(429, "SKILL_BUILDER_LIMIT_EXCEEDED", "quota"),
      ),
    ).toContain("会话已达到上限");
    expect(
      skillBuilderErrorMessage(
        new SkillBuilderApiError(
          503,
          "SKILL_BUILDER_UNAVAILABLE",
          "Asset storage unavailable",
        ),
      ),
    ).toBe("Skill 设计服务暂时不可用，请稍后重试。");
  });

  test("keeps drafts for multiple selected files and exposes all three list creation paths", () => {
    expect(
      skillBuilderDraftChanges(session.files, {
        "SKILL.md": "# Updated skill",
        "references/guide.md": "# Updated guide",
      }),
    ).toEqual([
      expect.objectContaining({ op: "replace", path: "SKILL.md" }),
      expect.objectContaining({
        op: "replace",
        path: "references/guide.md",
      }),
    ]);

    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/assets/project-asset-page-shell.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("AI 对话创建");
    expect(source).toContain("从空白创建");
    expect(source).toContain("上传压缩包");
    expect(source).toContain("/skills/new");
  });
});
