import { describe, expect, test } from "@rstest/core";

import { resolveSkillSecretConfigurationAccess } from "@/components/projects/assets/skill-asset-detail";

describe("Skill Configuration Secret lifecycle access", () => {
  test("Historical Project Skill permits clear but not replacement", () => {
    expect(
      resolveSkillSecretConfigurationAccess({
        scope: "project",
        selectedVersionId: "version-1",
        currentVersionId: "version-2",
        relation: "historical",
        canManageSecrets: true,
        systemBindingEnabled: false,
        systemBindingVersionId: null,
      }),
    ).toEqual({ visible: true, canReplace: false, canClear: true });
  });

  test("Current Project Skill permits both operations only with binding authority", () => {
    const common = {
      scope: "project" as const,
      selectedVersionId: "version-2",
      currentVersionId: "version-2",
      relation: "current" as const,
      systemBindingEnabled: false,
      systemBindingVersionId: null,
    };
    expect(
      resolveSkillSecretConfigurationAccess({
        ...common,
        canManageSecrets: true,
      }),
    ).toEqual({ visible: true, canReplace: true, canClear: true });
    expect(
      resolveSkillSecretConfigurationAccess({
        ...common,
        canManageSecrets: false,
      }),
    ).toEqual({ visible: true, canReplace: false, canClear: false });
  });
});
