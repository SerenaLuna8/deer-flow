import { describe, expect, test } from "@rstest/core";

import {
  skillBuilderMutationKey,
  skillBuilderRootKey,
  skillBuilderSessionKey,
  skillBuilderSessionsKey,
} from "@/core/skill-builder/query-keys";

describe("skill builder query keys", () => {
  test("keeps every key under the exact account and project root", () => {
    const root = skillBuilderRootKey("account-1", "project-1");
    expect(root).toEqual([
      "account",
      "account-1",
      "project",
      "project-1",
      "skill-builder",
    ]);
    expect(skillBuilderSessionsKey("account-1", "project-1")).toEqual([
      ...root,
      "sessions",
    ]);
    expect(
      skillBuilderSessionKey("account-1", "project-1", "session-1"),
    ).toEqual([...root, "sessions", "session-1"]);
    expect(
      skillBuilderMutationKey("account-1", "project-1", "validate"),
    ).toEqual([...root, "mutation", "validate"]);
  });
});
