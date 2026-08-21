import { describe, expect, test } from "@rstest/core";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import {
  projectAssetDetailDirty,
  projectAssetRequestedVersionResolution,
  projectSkillCredentialRepairVersionId,
  primaryVersionActionDisabled,
} from "@/components/projects/assets/project-asset-detail-sheet";
import {
  projectAssetCanDelete,
  projectSkillDeleteErrorMessage,
  projectSkillCredentialSetupRequired,
  projectSkillVersionCanActivate,
} from "@/components/projects/assets/project-asset-view-model";
import { projectAssetDeleteDescription } from "@/components/projects/assets/project-skill-delete-dialog";
import {
  PROJECT_SKILL_IMPORT_DESCRIPTION,
  projectSkillImportErrorMessage,
} from "@/components/projects/assets/project-skill-import-dialog";
import {
  skillCredentialBindingCanUnbind,
  skillCredentialBindingValidation,
  skillCredentialSelectionsAfterServerRefresh,
} from "@/components/projects/assets/skill-credential-bindings";
import {
  SharedAssetApiError,
  type SkillCredentialRequirement,
} from "@/core/shared-assets";

const EDIT = "shared_assets.edit" as const;
const MANAGE_BINDINGS = "shared_assets.manage_bindings" as const;
const CREDENTIAL_ID = "88888888-8888-4888-8888-888888888888";
const CREDENTIAL_VERSION_ID = "99999999-9999-4999-8999-999999999999";

function mappingRequirement(
  name: string,
  {
    optional = false,
    envFields = [name],
  }: { optional?: boolean; envFields?: string[] } = {},
): SkillCredentialRequirement {
  return {
    name,
    optional,
    configured: false,
    mapping_status: "missing",
    credential_id: null,
    credential_version_id: null,
    credential_display_name: null,
    credential_version_number: null,
    source_env_field_name: null,
    eligible_credentials: [
      {
        credential_id: CREDENTIAL_ID,
        credential_version_id: CREDENTIAL_VERSION_ID,
        display_name: "Shared Credential",
        version_number: 1,
        env_fields: envFields,
      },
    ],
  };
}

describe("Skill activation governance", () => {
  test("lets an Editor or Admin activate a Skill candidate", () => {
    const candidate = { relation: "candidate" as const };
    const editorItem = {
      scope: "project" as const,
      capabilities: [EDIT],
      current_version_id: null,
    };
    const adminItem = {
      ...editorItem,
      capabilities: [EDIT, MANAGE_BINDINGS],
    };

    expect(projectSkillVersionCanActivate(editorItem, [EDIT], candidate)).toBe(
      true,
    );
    expect(
      projectSkillVersionCanActivate(
        adminItem,
        [EDIT, MANAGE_BINDINGS],
        candidate,
      ),
    ).toBe(true);
    expect(
      projectSkillVersionCanActivate(adminItem, [EDIT, MANAGE_BINDINGS], {
        relation: "current",
      }),
    ).toBe(false);
  });

  test("keeps activation disabled until the server validates SKILL.md", () => {
    expect(primaryVersionActionDisabled(false, false, false, true)).toBe(true);
    expect(primaryVersionActionDisabled(false, false, false, false)).toBe(
      false,
    );
  });

  test("lets an Editor delete a Skill asset regardless of Current Version", () => {
    expect(
      projectAssetCanDelete("skills", {
        scope: "project",
        capabilities: [EDIT],
        current_version_id: null,
      }),
    ).toBe(true);
    expect(
      projectAssetCanDelete("skills", {
        scope: "project",
        capabilities: [EDIT],
        current_version_id: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(true);
    expect(
      projectAssetCanDelete("skills", {
        scope: "project",
        capabilities: [EDIT, MANAGE_BINDINGS],
        current_version_id: "11111111-1111-4111-8111-111111111111",
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

  test("describes archive import as a Candidate Version without publication terms", () => {
    expect(PROJECT_SKILL_IMPORT_DESCRIPTION).toContain("候选版本");
    expect(PROJECT_SKILL_IMPORT_DESCRIPTION).toContain("激活");
    expect(PROJECT_SKILL_IMPORT_DESCRIPTION).not.toMatch(/发布|草稿/u);
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

  test("blocks a partially completed Current mapping set but lets a Candidate save complete rows", () => {
    const requirements = [
      mappingRequirement("API_KEY"),
      mappingRequirement("DATABASE_URL"),
    ];
    const selections = {
      API_KEY: {
        credentialVersionId: CREDENTIAL_VERSION_ID,
        sourceEnvFieldName: "API_KEY",
      },
    };

    expect(
      skillCredentialBindingValidation(requirements, selections, true),
    ).toMatchObject({
      hasInvalidSelection: false,
      hasBlockingMissingRequired: true,
    });
    expect(
      skillCredentialBindingValidation(requirements, selections, false),
    ).toMatchObject({
      hasInvalidSelection: false,
      hasBlockingMissingRequired: false,
    });
  });

  test("blocks a stale source env field until it is replaced or optionally unbound", () => {
    const requirement = mappingRequirement("DATABASE_URL", {
      optional: true,
      envFields: ["NEW_DATABASE_URL"],
    });

    expect(
      skillCredentialBindingValidation(
        [requirement],
        {
          DATABASE_URL: {
            credentialVersionId: CREDENTIAL_VERSION_ID,
            sourceEnvFieldName: "OLD_DATABASE_URL",
          },
        },
        false,
      ),
    ).toMatchObject({
      statuses: { DATABASE_URL: "invalid" },
      hasInvalidSelection: true,
    });
    expect(
      skillCredentialBindingValidation([requirement], {}, false),
    ).toMatchObject({
      statuses: { DATABASE_URL: "missing" },
      hasInvalidSelection: false,
    });
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
          API_KEY: {
            credentialVersionId: "22222222-2222-4222-8222-222222222222",
            sourceEnvFieldName: "API_TOKEN",
          },
          REGION: {
            credentialVersionId: "33333333-3333-4333-8333-333333333333",
            sourceEnvFieldName: "REGION",
          },
          REMOVED: {
            credentialVersionId: "44444444-4444-4444-8444-444444444444",
            sourceEnvFieldName: "REMOVED",
          },
        },
        {
          API_KEY: {
            credentialVersionId: "11111111-1111-4111-8111-111111111111",
            sourceEnvFieldName: "API_KEY",
          },
          REGION: {
            credentialVersionId: "33333333-3333-4333-8333-333333333333",
            sourceEnvFieldName: "REGION",
          },
          REMOVED: {
            credentialVersionId: "44444444-4444-4444-8444-444444444444",
            sourceEnvFieldName: "REMOVED",
          },
        },
        {
          API_KEY: {
            credentialVersionId: "55555555-5555-4555-8555-555555555555",
            sourceEnvFieldName: "API_KEY",
          },
          REGION: {
            credentialVersionId: "66666666-6666-4666-8666-666666666666",
            sourceEnvFieldName: "REGION",
          },
          ADDED: {
            credentialVersionId: "77777777-7777-4777-8777-777777777777",
            sourceEnvFieldName: "ADDED_SOURCE",
          },
        },
        ["API_KEY", "REGION", "ADDED"],
      ),
    ).toEqual({
      selections: {
        API_KEY: {
          credentialVersionId: "22222222-2222-4222-8222-222222222222",
          sourceEnvFieldName: "API_TOKEN",
        },
        REGION: {
          credentialVersionId: "66666666-6666-4666-8666-666666666666",
          sourceEnvFieldName: "REGION",
        },
        ADDED: {
          credentialVersionId: "77777777-7777-4777-8777-777777777777",
          sourceEnvFieldName: "ADDED_SOURCE",
        },
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

  test("keeps an exact Candidate Version request while refreshed history is still arriving", () => {
    const current = "11111111-1111-4111-8111-111111111111";
    const candidate = "22222222-2222-4222-8222-222222222222";
    const resolve = projectAssetRequestedVersionResolution as (
      versions: readonly { id: string }[],
      requestedVersionId: string | null,
      historyReady: boolean,
      historyRefreshing: boolean,
    ) => ReturnType<typeof projectAssetRequestedVersionResolution>;

    expect(resolve([{ id: current }], candidate, true, true)).toBe("pending");
    expect(
      resolve([{ id: candidate }, { id: current }], candidate, true, false),
    ).toBe("available");
  });
});
