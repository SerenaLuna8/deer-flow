import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/private-work/readiness", () => ({
  useProjectPrivateWorkReadiness: rs.fn(),
}));
rs.mock("@/core/static-mode", () => ({
  isStaticWebsiteOnly: rs.fn(() => false),
}));
rs.mock("@/core/threads/hooks", () => ({
  useThreads: rs.fn(() => ({ data: [] })),
}));

import {
  RecentPrivateWork,
  RecentPrivateWorkView,
} from "@/components/projects/private-work/recent-private-work";
import { useProjectPrivateWorkReadiness } from "@/core/private-work/readiness";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { useThreads } from "@/core/threads/hooks";
import type { AgentThread } from "@/core/threads/types";

const project: Project = {
  id: "10000000-0000-4000-8000-000000000001",
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "viewer",
  capabilities: ["project.read", "private_work.read_own"],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

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

  test("does not query or render recent work while readiness is unavailable", () => {
    const status = "unavailable" as const;
    rs.mocked(isStaticWebsiteOnly).mockReturnValue(false);
    rs.mocked(useProjectPrivateWorkReadiness).mockReturnValue({
      data: { status, code: status, request_id: `request-${status}` },
    } as never);

    const html = renderToStaticMarkup(<RecentPrivateWork project={project} />);

    expect(html).toBe("");
    expect(useThreads).toHaveBeenLastCalledWith(expect.any(Object), undefined, {
      enabled: false,
    });
  });

  test("does not enable readiness or Thread search in a static build", () => {
    rs.mocked(isStaticWebsiteOnly).mockReturnValue(true);
    rs.mocked(useProjectPrivateWorkReadiness).mockReturnValue({} as never);

    const html = renderToStaticMarkup(<RecentPrivateWork project={project} />);

    expect(html).toBe("");
    expect(useProjectPrivateWorkReadiness).toHaveBeenLastCalledWith(false);
    expect(useThreads).toHaveBeenLastCalledWith(expect.any(Object), undefined, {
      enabled: false,
    });
  });
});
