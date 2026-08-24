import { describe, expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectSkillImportForm,
  projectSkillImportErrorMessage,
  resolveProjectSkillArchiveRiskConfirmation,
} from "@/components/projects/assets/project-skill-import-dialog";
import { SharedAssetApiError } from "@/core/shared-assets";

describe("Project Skill archive import errors", () => {
  test("shows actionable locations for a security-blocked upload", () => {
    const error = new SharedAssetApiError(
      422,
      "SKILL_ARCHIVE_SECURITY_BLOCKED",
      "Skill archive failed security scan",
      undefined,
      [
        {
          rule_id: "python-shell-exec",
          file: "scripts/run.py",
          line: 2,
        },
        {
          rule_id: "python-env-dump-exfil",
          file: "scripts/server_common.py",
          line: 85,
        },
      ],
      {
        acceptance: "accept-blocked-skill-archive",
        payload_checksum: "a".repeat(64),
        findings_checksum: "b".repeat(64),
      },
    );

    expect(projectSkillImportErrorMessage(error)).toBe(
      "Skill 压缩包存在以下安全风险：\n" +
        "确认后仅保存为受阻候选版本，修复阻断项前不能激活。\n" +
        "- python-shell-exec（scripts/run.py:2）\n" +
        "- python-env-dump-exfil（scripts/server_common.py:85）",
    );
  });

  test("keeps generic validation failures distinct from security blocks", () => {
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

  test("offers an explicit blocked-candidate upload only with server confirmation", () => {
    const baseProps = {
      selectedFile: { name: "blocked-upload.zip", size: 1024 },
      inputResetKey: 0,
      pending: false,
      errorMessage: "Skill 压缩包未通过安全扫描。",
      onFileChange: () => undefined,
      onSelectionChange: () => undefined,
      onSubmit: () => undefined,
    };
    const regular = renderToStaticMarkup(
      createElement(ProjectSkillImportForm, {
        ...baseProps,
        securityRiskConfirmation: null,
      }),
    );
    const confirmable = renderToStaticMarkup(
      createElement(ProjectSkillImportForm, {
        ...baseProps,
        securityRiskConfirmation: {
          acceptance: "accept-blocked-skill-archive",
          payload_checksum: "a".repeat(64),
          findings_checksum: "b".repeat(64),
        },
      }),
    );
    const confirming = renderToStaticMarkup(
      createElement(ProjectSkillImportForm, {
        ...baseProps,
        pending: true,
        securityRiskConfirmation: {
          acceptance: "accept-blocked-skill-archive",
          payload_checksum: "a".repeat(64),
          findings_checksum: "b".repeat(64),
        },
      }),
    );

    expect(regular).toContain("上传并创建");
    expect(regular).not.toContain("确认风险仍然上传");
    expect(confirmable).toContain("确认风险仍然上传");
    expect(confirmable).not.toContain("上传并创建");
    expect(confirming).toContain("确认上传中…");
    expect(confirming).not.toContain("上传并校验中…");
  });

  test("keeps the submitted confirmation while its retry is pending", () => {
    const confirmation = {
      acceptance: "accept-blocked-skill-archive" as const,
      payload_checksum: "a".repeat(64),
      findings_checksum: "b".repeat(64),
    };

    expect(
      resolveProjectSkillArchiveRiskConfirmation(null, true, confirmation),
    ).toEqual(confirmation);
    expect(resolveProjectSkillArchiveRiskConfirmation(null, true)).toBeNull();
    expect(
      resolveProjectSkillArchiveRiskConfirmation(
        new SharedAssetApiError(
          422,
          "SKILL_ARCHIVE_SECURITY_BLOCKED",
          "Skill archive failed security scan",
          undefined,
          [
            {
              rule_id: "python-shell-exec",
              file: "scripts/run.py",
              line: 2,
            },
          ],
          confirmation,
        ),
        false,
      ),
    ).toEqual(confirmation);
  });
});
