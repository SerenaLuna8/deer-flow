import { describe, expect, test } from "@rstest/core";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import {
  projectAssetDetailDirty,
  projectAssetRequestedVersionResolution,
  projectSkillCredentialRepairVersionId,
  versionPublishDisabled,
} from "@/components/projects/assets/project-asset-detail-sheet";
import {
  projectAssetCanDelete,
  projectSkillDeleteErrorMessage,
  projectSkillCredentialSetupRequired,
  projectSkillVersionCanPublish,
} from "@/components/projects/assets/project-asset-view-model";
import { projectAssetDeleteDescription } from "@/components/projects/assets/project-skill-delete-dialog";
import { projectSkillImportErrorMessage } from "@/components/projects/assets/project-skill-import-dialog";
import {
  skillCredentialBindingCanUnbind,
  skillCredentialSelectionsAfterServerRefresh,
} from "@/components/projects/assets/skill-credential-bindings";
import { SharedAssetApiError } from "@/core/shared-assets";

const EDIT = "shared_assets.edit" as const;
const MANAGE_BINDINGS = "shared_assets.manage_bindings" as const;

describe("Skill publisher governance", () => {
  test("requires binding-manager authority to publish a Skill draft", () => {
    const draft = { workflow_status: "draft" as const };
    const editorItem = {
      scope: "project" as const,
      capabilities: [EDIT],
      current_published_version_id: null,
    };
    const publisherItem = {
      ...editorItem,
      capabilities: [EDIT, MANAGE_BINDINGS],
    };

    expect(projectSkillVersionCanPublish(editorItem, [EDIT], draft)).toBe(
      false,
    );
    expect(
      projectSkillVersionCanPublish(
        publisherItem,
        [EDIT, MANAGE_BINDINGS],
        draft,
      ),
    ).toBe(true);
    expect(
      projectSkillVersionCanPublish(publisherItem, [EDIT, MANAGE_BINDINGS], {
        workflow_status: "published",
      }),
    ).toBe(false);
  });

  test("keeps publish disabled until the server validates SKILL.md", () => {
    expect(versionPublishDisabled(false, false, false, true)).toBe(true);
    expect(versionPublishDisabled(false, false, false, false)).toBe(false);
  });

  test("lets an editor delete only an unpublished Skill package", () => {
    expect(
      projectAssetCanDelete("skills", {
        scope: "project",
        capabilities: [EDIT],
        current_published_version_id: null,
      }),
    ).toBe(true);
    expect(
      projectAssetCanDelete("skills", {
        scope: "project",
        capabilities: [EDIT],
        current_published_version_id: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(false);
    expect(
      projectAssetCanDelete("skills", {
        scope: "project",
        capabilities: [EDIT, MANAGE_BINDINGS],
        current_published_version_id: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(true);
  });

  test("explains reference blockers before and after a failed delete", () => {
    const confirmation = projectAssetDeleteDescription(
      "Skill",
      "catalog-auditor",
    );
    expect(confirmation).toContain("Agent 或历史运行");
    expect(confirmation).toContain("可先停用以阻止后续使用");
    expect(confirmation).toContain("等待历史运行按保留策略清理");

    const failure = projectSkillDeleteErrorMessage(
      new SharedAssetApiError(409, "ASSET_IN_USE", "Asset is still referenced"),
    );
    expect(failure).toContain("仍有 Agent 或历史运行引用");
    expect(failure).toContain("可先停用以阻止后续使用");
    expect(failure).toContain("等待历史运行按保留策略清理");
  });

  test("shows a recoverable runtime-name conflict message", () => {
    expect(
      adminAssetErrorMessage(
        new SharedAssetApiError(
          409,
          "SKILL_RUNTIME_NAME_CONFLICT",
          "Skill runtime name conflict",
        ),
      ),
    ).toBe("与已启用 Skill 的运行名称冲突，请先停用其中一个 Skill 后重试。");
  });

  test("keeps the dedicated upload-limit copy distinct from invalid archives", () => {
    expect(
      projectSkillImportErrorMessage(
        new SharedAssetApiError(
          413,
          "ASSET_UPLOAD_TOO_LARGE",
          "Skill archive upload too large",
        ),
      ),
    ).toContain("超过上传或解压限制");
    expect(
      projectSkillImportErrorMessage(
        new SharedAssetApiError(
          422,
          "ASSET_VALIDATION_FAILED",
          "Asset validation failed",
        ),
      ),
    ).toContain("压缩包无效或格式不受支持");
  });

  test("does not let an active Skill remove a required binding", () => {
    expect(skillCredentialBindingCanUnbind(true, { optional: false })).toBe(
      false,
    );
    expect(skillCredentialBindingCanUnbind(true, { optional: true })).toBe(
      true,
    );
    expect(skillCredentialBindingCanUnbind(false, { optional: false })).toBe(
      true,
    );
  });

  test("turns incomplete activation into an actionable Credential repair", () => {
    const error = new SharedAssetApiError(
      422,
      "SKILL_CREDENTIAL_BINDINGS_INCOMPLETE",
      "Required Skill Credential bindings are incomplete",
    );

    expect(projectSkillCredentialSetupRequired(error)).toBe(true);
    expect(
      projectSkillCredentialRepairVersionId(
        error,
        "11111111-1111-4111-8111-111111111111",
      ),
    ).toBe("11111111-1111-4111-8111-111111111111");
    expect(adminAssetErrorMessage(error)).toContain("配置必需的 Credential");
    expect(
      projectSkillCredentialSetupRequired(
        new SharedAssetApiError(
          422,
          "ASSET_VALIDATION_FAILED",
          "Asset validation failed",
        ),
      ),
    ).toBe(false);
    expect(
      projectSkillCredentialRepairVersionId(
        new SharedAssetApiError(
          422,
          "ASSET_VALIDATION_FAILED",
          "Asset validation failed",
        ),
        "11111111-1111-4111-8111-111111111111",
      ),
    ).toBeNull();
  });

  test("merges a binding refetch without overwriting unsaved field edits", () => {
    expect(
      skillCredentialSelectionsAfterServerRefresh(
        {
          API_KEY: "22222222-2222-4222-8222-222222222222",
          REGION: "33333333-3333-4333-8333-333333333333",
          REMOVED: "44444444-4444-4444-8444-444444444444",
        },
        {
          API_KEY: "11111111-1111-4111-8111-111111111111",
          REGION: "33333333-3333-4333-8333-333333333333",
          REMOVED: "44444444-4444-4444-8444-444444444444",
        },
        {
          API_KEY: "55555555-5555-4555-8555-555555555555",
          REGION: "66666666-6666-4666-8666-666666666666",
          ADDED: "77777777-7777-4777-8777-777777777777",
        },
        ["API_KEY", "REGION", "ADDED"],
      ),
    ).toEqual({
      selections: {
        API_KEY: "22222222-2222-4222-8222-222222222222",
        REGION: "66666666-6666-4666-8666-666666666666",
        ADDED: "77777777-7777-4777-8777-777777777777",
      },
      preservedLocalChanges: true,
    });
  });

  test("keeps detail navigation dirty while Credential bindings are unsaved", () => {
    expect(projectAssetDetailDirty(false, true)).toBe(true);
    expect(projectAssetDetailDirty(true, false)).toBe(true);
    expect(projectAssetDetailDirty(false, false)).toBe(false);
  });

  test("rejects an exact-version query only after history proves it missing", () => {
    const requested = "11111111-1111-4111-8111-111111111111";
    const available = "22222222-2222-4222-8222-222222222222";

    expect(projectAssetRequestedVersionResolution([], requested, false)).toBe(
      "pending",
    );
    expect(
      projectAssetRequestedVersionResolution(
        [{ id: available }],
        requested,
        true,
      ),
    ).toBe("missing");
    expect(
      projectAssetRequestedVersionResolution(
        [{ id: requested }],
        requested,
        true,
      ),
    ).toBe("available");
  });
});
