import type { AgentThread } from "@/core/threads";

import {
  SIDECAR_METADATA_KEY,
  isSidecarThread,
} from "./thread";

type SidecarThreadSearchClient = {
  threads: {
    search: (query: Record<string, unknown>) => Promise<AgentThread[]>;
  };
};

export async function findLatestSidecarThread({
  parentThreadId,
  apiClient,
}: {
  parentThreadId: string;
  apiClient: SidecarThreadSearchClient;
}): Promise<AgentThread | null> {
  const response = await apiClient.threads.search({
    metadata: {
      [SIDECAR_METADATA_KEY]: true,
      parent_thread_id: parentThreadId,
    },
    limit: 1,
    offset: 0,
    sortBy: "updated_at",
    sortOrder: "desc",
  });

  return (
    response.find(
      (thread) =>
        isSidecarThread(thread) &&
        thread.metadata?.parent_thread_id === parentThreadId,
    ) ?? null
  );
}
