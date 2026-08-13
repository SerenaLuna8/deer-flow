import { describe, expect, test } from "@rstest/core";

import { settingsNavigationItemClassName } from "@/components/workspace/settings/settings-dialog";

describe("settingsNavigationItemClassName", () => {
  test("uses blue selected and hover states", () => {
    expect(settingsNavigationItemClassName(true)).toContain(
      "bg-blue-50 text-blue-600 before:bg-blue-600",
    );
    expect(settingsNavigationItemClassName(false)).toContain(
      "hover:bg-blue-50",
    );
    expect(settingsNavigationItemClassName(false)).not.toContain(
      "hover:text-blue-600",
    );
  });
});
