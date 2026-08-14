import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  SkillBuilderCandidateWorkbench,
  skillBuilderRevisionDiff,
} from "@/components/projects/skills/skill-builder-candidate-workbench";
import { SkillBuilderFilesTrigger } from "@/components/projects/skills/skill-builder-files-trigger";
import { I18nProvider } from "@/core/i18n/context";
import type { SkillBuilderFile } from "@/core/skill-builder";

function renderUi(node: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

const SHA256 = "a".repeat(64);

function file(path = "SKILL.md"): SkillBuilderFile {
  const content = "# Skill\n";
  return {
    path,
    media_type: "text/markdown",
    size_bytes: new TextEncoder().encode(content).byteLength,
    sha256: SHA256,
    encoding: "utf-8",
    content,
  };
}

function workbench(
  overrides: { files?: SkillBuilderFile[]; onClose?: () => void } = {},
) {
  return (
    <SkillBuilderCandidateWorkbench
      files={overrides.files ?? [file()]}
      selectedPath="SKILL.md"
      draftContent="# Skill\n"
      displayMode="source"
      canAuthor
      readOnly={false}
      dirty={false}
      baselineStale={false}
      pending={false}
      validation={null}
      canValidate={false}
      canCommit={false}
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
      onClose={overrides.onClose}
    />
  );
}

describe("SkillBuilderFilesTrigger", () => {
  test("hides until candidate files exist", () => {
    expect(
      renderUi(
        <SkillBuilderFilesTrigger fileCount={0} onOpen={() => undefined} />,
      ),
    ).toBe("");
  });

  test("reopens the candidate package like the chat files bar", () => {
    const html = renderUi(
      <SkillBuilderFilesTrigger fileCount={2} onOpen={() => undefined} />,
    );
    expect(html).toContain('data-testid="skill-builder-files-trigger"');
    expect(html).toContain('aria-label="查看候选文件包"');
    expect(html).toContain("文件");
  });
});

describe("SkillBuilderCandidateWorkbench", () => {
  test("closes with a control matching the chat files panel", () => {
    const html = renderUi(workbench({ onClose: () => undefined }));
    expect(html).toContain("候选文件包");
    expect(html).toContain('aria-label="关闭候选文件包"');
  });

  test("does not render a close control when the panel cannot be dismissed", () => {
    const html = renderUi(workbench());
    expect(html).not.toContain('aria-label="关闭候选文件包"');
  });

  test("shows revision diff badges against the pinned base files", () => {
    const html = renderUi(
      <SkillBuilderCandidateWorkbench
        files={[
          file("SKILL.md"),
          {
            ...file("notes.md"),
            sha256: "c".repeat(64),
          },
        ]}
        selectedPath="SKILL.md"
        draftContent="# Skill\n"
        displayMode="source"
        canAuthor
        readOnly={false}
        dirty={false}
        baselineStale={false}
        pending={false}
        validation={null}
        canValidate={false}
        canCommit
        acknowledgeWarnings={false}
        errorMessage={null}
        sessionKind="revise"
        baseVersionNumber={2}
        baseFiles={[
          {
            path: "SKILL.md",
            media_type: "text/markdown",
            size_bytes: new TextEncoder().encode("# Skill\n").byteLength,
            sha256: SHA256,
          },
          {
            path: "legacy.md",
            media_type: "text/markdown",
            size_bytes: 8,
            sha256: "d".repeat(64),
          },
        ]}
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

    expect(html).toContain("候选文件包（修订）");
    expect(html).toContain("较基线 v2：新增 1 · 修改 0 · 删除 1");
    expect(html).toContain("已从基线删除");
    expect(html).toContain("legacy.md");
    expect(html).toContain("创建新版本（待发布）");
  });
});

describe("skillBuilderRevisionDiff", () => {
  test("treats media_type-only changes as modifications", () => {
    const diff = skillBuilderRevisionDiff(
      [
        {
          path: "SKILL.md",
          sha256: SHA256,
          media_type: "text/plain",
          size_bytes: 8,
        },
      ],
      [
        {
          path: "SKILL.md",
          sha256: SHA256,
          media_type: "text/markdown",
          size_bytes: 8,
        },
      ],
    );

    expect(diff).toMatchObject({ added: 0, modified: 1, deleted: 0 });
    expect(diff.stateByPath.get("SKILL.md")).toBe("modified");
  });
});
