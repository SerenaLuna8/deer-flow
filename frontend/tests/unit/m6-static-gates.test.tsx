import { describe, expect, test } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import { projectNavigationItems } from "@/components/projects/project-nav";
import { projectStreamCursorStorageKey } from "@/core/private-work/api-client";
import { privateWorkRoot } from "@/core/private-work/query-keys";
import {
  createPrivateWorkScopeRegistry,
  transitionPrivateWorkScope,
} from "@/core/private-work/scope-registry";
import type { Project } from "@/core/projects/types";

const ACCOUNT_A = "11111111-1111-4111-8111-111111111111";
const ACCOUNT_B = "22222222-2222-4222-8222-222222222222";
const PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const project: Project = {
  id: PROJECT_A,
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.execute",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "m6-static-gate",
};

const scopeA = { accountId: ACCOUNT_A, projectId: PROJECT_A };
const scopeB = { accountId: ACCOUNT_B, projectId: PROJECT_B };

describe("M6 static, capability, readiness and cache-isolation release gates", () => {
  test("static export never exposes private-work URLs even with a ready server snapshot", () => {
    const hrefs = projectNavigationItems(
      project,
      true,
      true,
      true,
      true,
      true,
      true,
      true,
    ).map((item) => item.href);

    expect(hrefs).not.toContain("/projects/alpha/chats");
    expect(hrefs).not.toContain("/projects/alpha/memory");
    expect(hrefs).not.toContain("/projects/alpha/connections");
    expect(hrefs).not.toContain("/projects/alpha/automations");
    expect(hrefs).not.toContain("/projects/alpha/settings/usage");
    expect(hrefs).not.toContain("/projects/alpha/settings/audit");
  });

  test("private-work navigation requires both capability and live readiness", () => {
    const ready = projectNavigationItems(project, true).map((item) => item.href);
    const unavailable = projectNavigationItems(project, false).map(
      (item) => item.href,
    );
    const denied = projectNavigationItems(
      { ...project, capabilities: ["project.read"] },
      true,
    ).map((item) => item.href);

    expect(ready).toContain("/projects/alpha/chats");
    expect(unavailable).not.toContain("/projects/alpha/chats");
    expect(denied).not.toContain("/projects/alpha/chats");
  });

  test("stream cursors and query roots are isolated by account and project", () => {
    expect(projectStreamCursorStorageKey(scopeA, "thread-1")).not.toBe(
      projectStreamCursorStorageKey(scopeB, "thread-1"),
    );
    expect(privateWorkRoot(scopeA)).not.toEqual(privateWorkRoot(scopeB));
  });

  test("account transition removes old project queries and mutations", async () => {
    const queryClient = new QueryClient();
    const registry = createPrivateWorkScopeRegistry();
    registry.acquire(scopeA);
    queryClient.setQueryData([...privateWorkRoot(scopeA), "threads"], [
      "secret-a",
    ]);
    queryClient.setMutationDefaults([...privateWorkRoot(scopeA), "mutation"], {
      mutationFn: async () => "secret-a",
    });

    await transitionPrivateWorkScope(registry, queryClient, scopeA, scopeB);

    expect(
      queryClient.getQueriesData({ queryKey: privateWorkRoot(scopeA) }),
    ).toEqual([]);
    expect(registry.has(scopeA)).toBe(false);
  });
});
