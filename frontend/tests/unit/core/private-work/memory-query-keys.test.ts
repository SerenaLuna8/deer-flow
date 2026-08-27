import { describe, expect, test } from "@rstest/core";

import {
  projectMemoryDocumentQueryKey,
  projectMemoryDreamPreparationQueryKey,
} from "@/core/private-work/memory/query-keys";

describe("Project Memory query keys", () => {
  test("keeps account/project rooted coordinates unchanged", () => {
    const scope = {
      accountId: "11111111-1111-4111-8111-111111111111",
      projectId: "22222222-2222-4222-8222-222222222222",
    };

    expect(projectMemoryDocumentQueryKey(scope)).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
      "memory",
      "document",
    ]);
    const jobId = "33333333-3333-4333-8333-333333333333";
    expect(projectMemoryDreamPreparationQueryKey(scope, jobId)).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
      "memory",
      "dream-preparation",
      jobId,
    ]);
    expect(() =>
      projectMemoryDreamPreparationQueryKey(scope, "not-a-job-id"),
    ).toThrow();
  });
});
