import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillBuilderCandidateWorkbench } from "@/components/projects/skills/skill-builder-candidate-workbench";
import { SkillBuilderFilesTrigger } from "@/components/projects/skills/skill-builder-files-trigger";
import type { SkillBuilderFile } from "@/core/skill-builder";

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
      renderToStaticMarkup(
        <SkillBuilderFilesTrigger fileCount={0} onOpen={() => undefined} />,
      ),
    ).toBe("");
  });

  test("reopens the candidate package like the chat files bar", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderFilesTrigger fileCount={2} onOpen={() => undefined} />,
    );
    expect(html).toContain('data-testid="skill-builder-files-trigger"');
    expect(html).toContain('aria-label="查看候选文件包"');
    expect(html).toContain("文件");
  });
});

describe("SkillBuilderCandidateWorkbench", () => {
  test("closes with a control matching the chat files panel", () => {
    const html = renderToStaticMarkup(workbench({ onClose: () => undefined }));
    expect(html).toContain("候选文件包");
    expect(html).toContain('aria-label="关闭候选文件包"');
  });

  test("does not render a close control when the panel cannot be dismissed", () => {
    const html = renderToStaticMarkup(workbench());
    expect(html).not.toContain('aria-label="关闭候选文件包"');
  });
});
