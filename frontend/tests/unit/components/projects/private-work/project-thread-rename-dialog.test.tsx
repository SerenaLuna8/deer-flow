import { describe, expect, test } from "@rstest/core";

import {
  PROJECT_THREAD_TITLE_MAX_LENGTH,
  projectThreadRenameTitleState,
} from "@/components/projects/private-work/project-thread-rename-dialog";

describe("project thread rename title", () => {
  test("accepts the exact limit and rejects the reported 240-character title", () => {
    expect(
      projectThreadRenameTitleState(
        "X".repeat(PROJECT_THREAD_TITLE_MAX_LENGTH),
      ),
    ).toMatchObject({
      canSubmit: true,
      characterCount: PROJECT_THREAD_TITLE_MAX_LENGTH,
      tooLong: false,
    });
    expect(projectThreadRenameTitleState("X".repeat(240))).toMatchObject({
      canSubmit: false,
      characterCount: 240,
      tooLong: true,
    });
  });

  test("counts user-visible Unicode characters instead of UTF-16 code units", () => {
    expect(
      projectThreadRenameTitleState(
        "😀".repeat(PROJECT_THREAD_TITLE_MAX_LENGTH),
      ),
    ).toMatchObject({
      canSubmit: true,
      characterCount: PROJECT_THREAD_TITLE_MAX_LENGTH,
      tooLong: false,
    });
  });

  test("trims only the value submitted after validation", () => {
    expect(projectThreadRenameTitleState("  concise title  ")).toMatchObject({
      canSubmit: true,
      normalizedTitle: "concise title",
      tooLong: false,
    });
    expect(projectThreadRenameTitleState("   ")).toMatchObject({
      canSubmit: false,
      normalizedTitle: "",
    });
  });
});
