import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { RecentPrivateWorkView } from "@/components/projects/private-work/recent-private-work";
import type { AgentThread } from "@/core/threads/types";

function thread(threadId: string, title: string): AgentThread {
  return {
    thread_id: threadId,
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T01:00:00Z",
    metadata: {},
    status: "idle",
    values: { title, messages: [] },
    interrupts: {},
  };
}

describe("recent project private work", () => {
  test("uses the current project route for owner-scoped threads", () => {
    const ownThread = thread(
      "11111111-1111-4111-8111-111111111111",
      "My private research",
    );
    const html = renderToStaticMarkup(
      <RecentPrivateWorkView projectSlug="alpha team" threads={[ownThread]} />,
    );

    expect(html).toContain("My private research");
    expect(html).toContain(
      "/projects/alpha%20team/chats/11111111-1111-4111-8111-111111111111",
    );
    expect(html).not.toContain("/workspace/chats/");
  });

  test("renders an explicit empty state", () => {
    const html = renderToStaticMarkup(
      <RecentPrivateWorkView projectSlug="alpha" threads={[]} />,
    );
    expect(html).toContain("还没有私有对话");
  });
});
