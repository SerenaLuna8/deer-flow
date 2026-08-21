import { describe, expect, test } from "@rstest/core";

import { skillExportBlockReason } from "@/components/assets/skill-export-button";

describe("Skill export availability", () => {
  test("blocks unsaved edits before all other states", () => {
    expect(
      skillExportBlockReason({
        hasVersion: true,
        unsaved: true,
        loading: true,
        revoked: true,
      }),
    ).toBe("unsaved");
  });

  test("blocks revoked versions and permits persisted eligible versions", () => {
    expect(skillExportBlockReason({ hasVersion: true, revoked: true })).toBe(
      "revoked",
    );
    expect(skillExportBlockReason({ hasVersion: true })).toBeNull();
    expect(skillExportBlockReason({ hasVersion: false })).toBe("no-version");
  });

  test("blocks a System Skill version that is not Current", () => {
    expect(skillExportBlockReason({ hasVersion: true, notCurrent: true })).toBe(
      "not-current",
    );
  });
});
