import { describe, expect, it } from "@rstest/core";

import { SETTINGS_SECTION_IDS } from "@/components/workspace/settings/settings-sections";

describe("SETTINGS_SECTION_IDS", () => {
  it("excludes capabilities promoted to workspace navigation", () => {
    expect(SETTINGS_SECTION_IDS).toEqual([
      "account",
      "appearance",
      "notification",
      "channels",
      "about",
    ]);
    expect(SETTINGS_SECTION_IDS).not.toContain("memory");
    expect(SETTINGS_SECTION_IDS).not.toContain("tools");
    expect(SETTINGS_SECTION_IDS).not.toContain("skills");
  });
});
