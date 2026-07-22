import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectAgentStartContinuationView,
  consumeProjectStartChatIntent,
  projectStartChatCandidate,
} from "@/components/projects/private-work/project-agent-start-continuation";
import type { ProjectAssetList } from "@/core/shared-assets";

const VERSION_ID = "44444444-4444-4444-8444-444444444444";
const catalog: ProjectAssetList = {
  project_items: [
    {
      id: "22222222-2222-4222-8222-222222222222",
      scope: "project",
      project_id: "11111111-1111-4111-8111-111111111111",
      slug: "analyst",
      display_name: "Analyst",
      status: "active",
      current_published_version_id: VERSION_ID,
      version: 2,
      created_by_user_id: "user-1",
      created_at: "2026-07-15T00:00:00Z",
      updated_at: "2026-07-15T00:00:00Z",
      capabilities: ["shared_assets.read", "shared_assets.execute"],
      binding: null,
    },
  ],
  system_items: [],
  request_id: "request-agents",
};
describe("project Agent start-chat continuation", () => {
  test("selects an executable Agent only for an authorized ready start_chat intent", () => {
    expect(
      projectStartChatCandidate(catalog, {
        requested: true,
        canCreate: true,
        readinessStatus: "ready",
      })?.id,
    ).toBe(catalog.project_items[0]!.id);
    for (const state of [
      { requested: false, canCreate: true, readinessStatus: "ready" as const },
      { requested: true, canCreate: false, readinessStatus: "ready" as const },
      {
        requested: true,
        canCreate: true,
        readinessStatus: "unavailable" as const,
      },
    ]) {
      expect(projectStartChatCandidate(catalog, state)).toBeNull();
    }
  });

  test("explains preserved intent while configuration is incomplete", () => {
    const waiting = renderToStaticMarkup(
      <ProjectAgentStartContinuationView status="waiting-for-agent" />,
    );
    expect(waiting).toContain("完成 Agent 配置后将自动创建对话");

    const creating = renderToStaticMarkup(
      <ProjectAgentStartContinuationView status="creating-chat" />,
    );
    expect(creating).toContain("正在创建你的首个对话");

    const forbidden = renderToStaticMarkup(
      <ProjectAgentStartContinuationView status="read-only" />,
    );
    expect(forbidden).toContain("你可以查看 Agent，但不能创建新的私有对话");
  });

  test("creates the first Chat and consumes start_chat with replace navigation", async () => {
    const calls: unknown[] = [];
    const agent = projectStartChatCandidate(catalog, {
      requested: true,
      canCreate: true,
      readinessStatus: "ready",
    })!;

    await consumeProjectStartChatIntent({
      scope: {
        accountId: "99999999-9999-4999-8999-999999999999",
        projectId: "11111111-1111-4111-8111-111111111111",
      },
      projectSlug: "alpha",
      intentId: "intent-first",
      agent,
      createChat: async (input) => {
        calls.push(["create", input.agent.id]);
        input.navigate("/projects/alpha/chats/first");
        return "first";
      },
      replace: (path) => calls.push(["replace", path]),
    });

    expect(calls).toEqual([
      ["create", agent.id],
      ["replace", "/projects/alpha/chats/first"],
    ]);
  });

  test("coalesces the same start_chat intent across project-shell remounts", async () => {
    const agent = projectStartChatCandidate(catalog, {
      requested: true,
      canCreate: true,
      readinessStatus: "ready",
    })!;
    let releaseCreate: ((threadId: string) => void) | undefined;
    const pendingCreate = new Promise<string>((resolve) => {
      releaseCreate = resolve;
    });
    let createCount = 0;
    const replacements: string[] = [];
    const shared = {
      scope: {
        accountId: "99999999-9999-4999-8999-999999999999",
        projectId: "11111111-1111-4111-8111-111111111111",
      },
      projectSlug: "alpha",
      intentId: "intent-remount",
      agent,
      createChat: async () => {
        createCount += 1;
        return pendingCreate;
      },
    };

    const beforeRemount = consumeProjectStartChatIntent({
      ...shared,
      replace: (path) => replacements.push(`first:${path}`),
    });
    const afterRemount = consumeProjectStartChatIntent({
      ...shared,
      replace: (path) => replacements.push(`second:${path}`),
    });

    await Promise.resolve();
    expect(createCount).toBe(1);
    releaseCreate?.("thread-once");
    await Promise.all([beforeRemount, afterRemount]);
    expect(replacements).toEqual([
      "first:/projects/alpha/chats/thread-once",
      "second:/projects/alpha/chats/thread-once",
    ]);
  });
});
