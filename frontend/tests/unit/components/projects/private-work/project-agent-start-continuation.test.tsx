import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectAgentStartContinuationView,
  consumeProjectStartChatIntent,
  projectStartChatCandidate,
} from "@/components/projects/private-work/project-agent-start-continuation";
import type {
  ProjectAssetList,
  ProjectDefaultAgent,
} from "@/core/shared-assets";

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
  system_items: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      scope: "system",
      project_id: null,
      slug: "project-assistant",
      display_name: "Main",
      status: "active",
      current_published_version_id: VERSION_ID,
      version: 1,
      created_by_user_id: "system",
      created_at: "2026-07-15T00:00:00Z",
      updated_at: "2026-07-15T00:00:00Z",
      capabilities: ["shared_assets.read", "shared_assets.execute"],
      binding: null,
    },
  ],
  request_id: "request-agents",
};
const mainDefault: ProjectDefaultAgent = {
  agent_asset_id: null,
  revision: 0,
  request_id: "request-default-main",
};
const projectDefault: ProjectDefaultAgent = {
  agent_asset_id: catalog.project_items[0]!.id,
  revision: 2,
  request_id: "request-default-project",
};
describe("project Agent start-chat continuation", () => {
  test("selects the project default or Main fallback for an authorized ready intent", () => {
    expect(
      projectStartChatCandidate(catalog, projectDefault, {
        requested: true,
        canCreate: true,
        readinessStatus: "ready",
      })?.id,
    ).toBe(catalog.project_items[0]!.id);
    expect(
      projectStartChatCandidate(catalog, mainDefault, {
        requested: true,
        canCreate: true,
        readinessStatus: "ready",
      })?.id,
    ).toBe(catalog.system_items[0]!.id);
    for (const state of [
      { requested: false, canCreate: true, readinessStatus: "ready" as const },
      { requested: true, canCreate: false, readinessStatus: "ready" as const },
      {
        requested: true,
        canCreate: true,
        readinessStatus: "unavailable" as const,
      },
    ]) {
      expect(projectStartChatCandidate(catalog, mainDefault, state)).toBeNull();
    }
  });

  test("fails closed when the configured default Agent is missing", () => {
    expect(
      projectStartChatCandidate(
        catalog,
        {
          agent_asset_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          revision: 3,
          request_id: "request-default-missing",
        },
        {
          requested: true,
          canCreate: true,
          readinessStatus: "ready",
        },
      ),
    ).toBeNull();
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

    const failed = renderToStaticMarkup(
      <ProjectAgentStartContinuationView
        status="error"
        errorMessage="该 MCP 版本当前不能作为 Agent 依赖"
      />,
    );
    expect(failed).toContain("该 MCP 版本当前不能作为 Agent 依赖");
  });

  test("creates the first Chat and consumes start_chat with replace navigation", async () => {
    const calls: unknown[] = [];
    await consumeProjectStartChatIntent({
      scope: {
        accountId: "99999999-9999-4999-8999-999999999999",
        projectId: "11111111-1111-4111-8111-111111111111",
      },
      projectSlug: "alpha",
      intentId: "intent-first",
      createChat: async (input) => {
        calls.push(["create", Object.hasOwn(input, "agent")]);
        input.navigate("/projects/alpha/chats/first");
        return "first";
      },
      replace: (path) => calls.push(["replace", path]),
    });

    expect(calls).toEqual([
      ["create", false],
      ["replace", "/projects/alpha/chats/first"],
    ]);
  });

  test("coalesces the same start_chat intent across project-shell remounts", async () => {
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
