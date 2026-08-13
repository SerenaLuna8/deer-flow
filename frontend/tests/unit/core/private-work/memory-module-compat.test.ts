import { describe, expect, test } from "@rstest/core";

import {
  admitProjectMemoryDreamPreparation as admitProjectMemoryDreamPreparationLegacy,
  getProjectMemory as getProjectMemoryLegacy,
  memoryDreamPreparationStatusSchema as memoryDreamPreparationStatusSchemaLegacy,
  memoryDocumentSchema as memoryDocumentSchemaLegacy,
  projectMemoryDreamPreparationQueryKey as projectMemoryDreamPreparationQueryKeyLegacy,
  projectMemoryDocumentQueryKey as projectMemoryDocumentQueryKeyLegacy,
} from "@/core/private-work/memory";
import {
  admitProjectMemoryDreamPreparation,
  getProjectMemory,
} from "@/core/private-work/memory/api";
import {
  projectMemoryDocumentQueryKey,
  projectMemoryDreamPreparationQueryKey,
} from "@/core/private-work/memory/query-keys";
import {
  memoryDocumentSchema,
  memoryDreamPreparationStatusSchema,
} from "@/core/private-work/memory/schemas";

describe("Project Memory module compatibility", () => {
  test("keeps legacy API, schema and query-key exports identical", () => {
    expect(getProjectMemory).toBe(getProjectMemoryLegacy);
    expect(memoryDocumentSchema).toBe(memoryDocumentSchemaLegacy);
    expect(projectMemoryDocumentQueryKey).toBe(
      projectMemoryDocumentQueryKeyLegacy,
    );
    expect(admitProjectMemoryDreamPreparation).toBe(
      admitProjectMemoryDreamPreparationLegacy,
    );
    expect(memoryDreamPreparationStatusSchema).toBe(
      memoryDreamPreparationStatusSchemaLegacy,
    );
    expect(projectMemoryDreamPreparationQueryKey).toBe(
      projectMemoryDreamPreparationQueryKeyLegacy,
    );
  });

  test("keeps account/project rooted query-key coordinates unchanged", () => {
    const scope = {
      accountId: "11111111-1111-4111-8111-111111111111",
      projectId: "22222222-2222-4222-8222-222222222222",
    };

    expect(projectMemoryDocumentQueryKey(scope)).toEqual(
      projectMemoryDocumentQueryKeyLegacy(scope),
    );
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
