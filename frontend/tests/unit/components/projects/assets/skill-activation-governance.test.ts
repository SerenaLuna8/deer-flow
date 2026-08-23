import { describe, expect, test } from "@rstest/core";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import {
  projectSkillSecretRepairVersionId,
  primaryVersionActionDisabled,
} from "@/components/projects/assets/project-asset-detail-sheet";
import {
  projectAssetCanDelete,
  projectSkillSecretSetupRequired,
  projectSkillVersionCanActivate,
} from "@/components/projects/assets/project-asset-view-model";
import { SharedAssetApiError } from "@/core/shared-assets";

const EDIT = "shared_assets.edit" as const;
const MANAGE_BINDINGS = "shared_assets.manage_bindings" as const;
const VERSION_ID = "11111111-1111-4111-8111-111111111111";

describe("Skill activation secret governance", () => {
  test("lets an editor activate a Candidate only after server readiness", () => {
    const item = {
      scope: "project" as const,
      capabilities: [EDIT, MANAGE_BINDINGS],
      current_version_id: null,
    };
    expect(
      projectSkillVersionCanActivate(item, item.capabilities, {
        relation: "candidate",
      }),
    ).toBe(true);
    expect(primaryVersionActionDisabled(false, false, false, true)).toBe(true);
    expect(primaryVersionActionDisabled(false, false, false, false)).toBe(
      false,
    );
  });

  test("keeps Project Skill deletion under shared_assets.edit", () => {
    expect(
      projectAssetCanDelete("skills", {
        scope: "project",
        capabilities: [EDIT],
        current_version_id: VERSION_ID,
      }),
    ).toBe(true);
  });

  test("turns incomplete secrets into an actionable exact-version repair", () => {
    const error = new SharedAssetApiError(
      422,
      "SKILL_SECRETS_INCOMPLETE",
      "Required Skill secrets are incomplete",
    );
    expect(projectSkillSecretSetupRequired(error)).toBe(true);
    expect(projectSkillSecretRepairVersionId(error, VERSION_ID)).toBe(
      VERSION_ID,
    );
    expect(adminAssetErrorMessage(error)).toContain("Skill 运行秘密");
    expect(
      projectSkillSecretSetupRequired(
        new SharedAssetApiError(
          422,
          "ASSET_VALIDATION_FAILED",
          "Asset validation failed",
        ),
      ),
    ).toBe(false);
  });
});
