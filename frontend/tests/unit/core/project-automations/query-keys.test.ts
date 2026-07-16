import { describe, expect, test } from "@rstest/core";

import {
  automationMutationKey,
  automationQueryKey,
  automationRoot,
} from "@/core/project-automations/query-keys";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

describe("project automation query keys", () => {
  test("keys every automation query by account and project", () => {
    expect(automationRoot(SCOPE)).toEqual([
      "account",
      SCOPE.accountId,
      "project",
      SCOPE.projectId,
      "automations",
    ]);
    expect(automationQueryKey(SCOPE, "task", "task-1")).toEqual([
      "account",
      SCOPE.accountId,
      "project",
      SCOPE.projectId,
      "automations",
      "task",
      "task-1",
    ]);
  });

  test("uses the same scoped root for mutation ownership", () => {
    expect(automationMutationKey(SCOPE, "trigger")).toEqual([
      "account",
      SCOPE.accountId,
      "project",
      SCOPE.projectId,
      "automations",
      "mutation",
      "trigger",
    ]);
  });

  test("rejects malformed account or project identities", () => {
    expect(() =>
      automationRoot({ ...SCOPE, accountId: "not-an-account" }),
    ).toThrow();
    expect(() =>
      automationRoot({ ...SCOPE, projectId: "not-a-project" }),
    ).toThrow();
  });
});
