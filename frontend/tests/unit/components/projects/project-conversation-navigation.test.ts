import { describe, expect, test } from "@rstest/core";

import { projectConversationAutoOpenPath } from "@/components/projects/private-work/project-conversation-rail";
import type { AgentThread } from "@/core/threads/types";

function thread(threadId: string, updatedAt?: string): AgentThread {
  return {
    thread_id: threadId,
    updated_at: updatedAt,
    values: { title: threadId, messages: [] },
    metadata: {},
  } as unknown as AgentThread;
}

describe("project conversation navigation", () => {
  test("opens the first server-ordered conversation only from the chats landing route", () => {
    const threads = [
      thread("server-first", "2026-08-05T08:00:00Z"),
      thread("server-second", "2026-08-07T08:00:00Z"),
    ];

    expect(
      projectConversationAutoOpenPath("alpha project", undefined, threads),
    ).toBe("/projects/alpha%20project/chats/server-first");
    expect(
      projectConversationAutoOpenPath("alpha project", "already-open", threads),
    ).toBeNull();
    expect(
      projectConversationAutoOpenPath("alpha project", undefined, []),
    ).toBeNull();
  });
});
