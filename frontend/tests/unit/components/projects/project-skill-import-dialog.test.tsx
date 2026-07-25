import { describe, expect, test } from "@rstest/core";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  PROJECT_SKILL_ARCHIVE_ACCEPT,
  ProjectSkillImportForm,
  isSupportedProjectSkillArchiveName,
  resolveProjectSkillArchiveSelection,
  projectSkillImportErrorMessage,
} from "@/components/projects/assets/project-skill-import-dialog";
import { SharedAssetApiError } from "@/core/shared-assets";

type TestElement = ReactElement<Record<string, unknown>>;

function findElement(
  node: ReactNode,
  predicate: (element: TestElement) => boolean,
): TestElement | null {
  if (isValidElement<Record<string, unknown>>(node)) {
    if (predicate(node)) return node;
    return findElement(node.props.children as ReactNode, predicate);
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      const nested = findElement(child, predicate);
      if (nested) return nested;
    }
  }
  return null;
}

describe("project Skill archive import dialog", () => {
  test("accepts exactly the archive suffixes supported by the project import API", () => {
    for (const name of [
      "meeting.zip",
      "meeting.skill",
      "meeting.tar",
      "meeting.tar.gz",
      "meeting.tgz",
      "MEETING.ZIP",
      "MEETING.SKILL",
      "MEETING.TAR.GZ",
    ]) {
      expect(isSupportedProjectSkillArchiveName(name)).toBe(true);
    }
    for (const name of [
      "meeting.gz",
      "meeting.rar",
      "meeting.7z",
      "meeting.gz",
      "meeting.zip.exe",
      "",
    ]) {
      expect(isSupportedProjectSkillArchiveName(name)).toBe(false);
    }
    expect(PROJECT_SKILL_ARCHIVE_ACCEPT).toContain(".zip");
    expect(PROJECT_SKILL_ARCHIVE_ACCEPT).toContain(".skill");
    expect(PROJECT_SKILL_ARCHIVE_ACCEPT).toContain(".tar.gz");
    expect(PROJECT_SKILL_ARCHIVE_ACCEPT).toContain(".tgz");
  });

  test("shows the selected archive and a stable uploading state", () => {
    const html = renderToStaticMarkup(
      <ProjectSkillImportForm
        selectedFile={{ name: "meeting-brief.skill", size: 2048 }}
        inputResetKey={0}
        pending
        errorMessage={null}
        onFileChange={() => undefined}
        onSelectionChange={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    expect(html).toContain("meeting-brief.skill");
    expect(html).toContain("2 KB");
    expect(html).toContain("上传并校验中…");
    expect(html).toContain(`accept="${PROJECT_SKILL_ARCHIVE_ACCEPT}"`);
    expect(html).toContain('disabled=""');
  });

  test("remounts the native file input and clears a stale mutation error when the selection changes", () => {
    const events: string[] = [];
    const tree = ProjectSkillImportForm({
      selectedFile: { name: "duplicate.skill", size: 2048 },
      inputResetKey: 7,
      pending: false,
      errorMessage: "当前项目已存在同名或同标识的 Skill，请更换压缩包后重试。",
      onFileChange: (file) =>
        events.push(file ? `file:${file.name}` : "file:null"),
      onSelectionChange: () => events.push("reset-error"),
      onSubmit: () => undefined,
    });
    const input = findElement(tree, (element) => element.props.type === "file");
    const remove = findElement(
      tree,
      (element) => element.props["aria-label"] === "移除已选择的压缩包",
    );
    expect(input?.key).toBe("7");
    expect(typeof input?.props.onChange).toBe("function");
    expect(typeof remove?.props.onClick).toBe("function");

    const replacement = new File(["replacement"], "replacement.skill");
    (
      input?.props.onChange as (event: {
        currentTarget: { files: { item: () => File } };
      }) => void
    )({ currentTarget: { files: { item: () => replacement } } });
    (remove?.props.onClick as () => void)();

    expect(events).toEqual([
      "reset-error",
      "file:replacement.skill",
      "reset-error",
      "file:null",
    ]);
    expect(resolveProjectSkillArchiveSelection(null)).toEqual({
      file: null,
      errorMessage: null,
      resetInput: true,
    });
  });

  test("uses actionable safe messages for duplicate invalid and oversized archives", () => {
    expect(
      projectSkillImportErrorMessage(
        new SharedAssetApiError(409, "ASSET_CONFLICT", "private duplicate"),
      ),
    ).toBe("当前项目已存在同名或同标识的 Skill，请更换压缩包后重试。");
    expect(
      projectSkillImportErrorMessage(
        new SharedAssetApiError(
          422,
          "ASSET_VALIDATION_FAILED",
          "private parser detail",
        ),
      ),
    ).toBe("压缩包无效或格式不受支持，请确认其中包含有效的 SKILL.md。");
    expect(
      projectSkillImportErrorMessage(
        new SharedAssetApiError(
          413,
          "ASSET_UPLOAD_TOO_LARGE",
          "private size detail",
        ),
      ),
    ).toBe("压缩包超过上传或解压限制，请缩小后重试。");
  });
});
