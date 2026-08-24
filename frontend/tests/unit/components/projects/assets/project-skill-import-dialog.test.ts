import { describe, expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectSkillImportForm,
  projectSkillImportErrorMessage,
} from "@/components/projects/assets/project-skill-import-dialog";
import { SharedAssetApiError } from "@/core/shared-assets";

describe("Project Skill archive import", () => {
  test("maps generic validation failures to the archive guidance", () => {
    expect(
      projectSkillImportErrorMessage(
        new SharedAssetApiError(
          422,
          "ASSET_VALIDATION_FAILED",
          "Asset validation failed",
        ),
      ),
    ).toBe("压缩包无效或格式不受支持，请确认其中包含有效的 SKILL.md。");
  });

  test("renders one ordinary upload action", () => {
    const baseProps = {
      selectedFile: { name: "skill.zip", size: 1024 },
      inputResetKey: 0,
      errorMessage: null,
      onFileChange: () => undefined,
      onSelectionChange: () => undefined,
      onSubmit: () => undefined,
    };
    const ready = renderToStaticMarkup(
      createElement(ProjectSkillImportForm, {
        ...baseProps,
        pending: false,
      }),
    );
    const pending = renderToStaticMarkup(
      createElement(ProjectSkillImportForm, {
        ...baseProps,
        pending: true,
      }),
    );

    expect(ready).toContain("上传并创建");
    expect(pending).toContain("上传并创建中…");
  });
});
