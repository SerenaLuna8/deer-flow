import { expect, rs, test } from "@rstest/core";

import { findLatestSidecarThread } from "@/core/sidecar/api";
import type { AgentThread } from "@/core/threads";

function makeThread(
  threadId: string,
  metadata: Record<string, unknown> = {},
): AgentThread {
  return {
    thread_id: threadId,
    created_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-01T00:00:00Z",
    metadata,
    status: "idle",
    values: { title: threadId, messages: [] },
  } as unknown as AgentThread;
}

test("finds the latest sidecar thread for a parent thread", async () => {
  const sidecar = makeThread("sidecar-1", {
    deerflow_sidecar: true,
    parent_thread_id: "parent-1",
  });
  const search = rs.fn().mockResolvedValue([sidecar]);

  await expect(
    findLatestSidecarThread({
      parentThreadId: "parent-1",
      apiClient: { threads: { search } },
    }),
  ).resolves.toBe(sidecar);

  expect(search).toHaveBeenCalledWith({
    metadata: {
      deerflow_sidecar: true,
      parent_thread_id: "parent-1",
    },
    limit: 1,
    offset: 0,
    sortBy: "updated_at",
    sortOrder: "desc",
  });
});

test("ignores malformed sidecar search results", async () => {
  const search = rs.fn().mockResolvedValue([makeThread("primary-1")]);

  await expect(
    findLatestSidecarThread({
      parentThreadId: "parent-1",
      apiClient: { threads: { search } },
    }),
  ).resolves.toBeNull();
});

test("ignores sidecar search results from another parent thread", async () => {
  const search = rs.fn().mockResolvedValue([
    makeThread("sidecar-1", {
      deerflow_sidecar: true,
      parent_thread_id: "parent-2",
    }),
  ]);

  await expect(
    findLatestSidecarThread({
      parentThreadId: "parent-1",
      apiClient: { threads: { search } },
    }),
  ).resolves.toBeNull();
});
