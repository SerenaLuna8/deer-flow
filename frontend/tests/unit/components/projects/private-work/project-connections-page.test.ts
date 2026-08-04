import { describe, expect, test } from "@rstest/core";

import {
  bindingCodeCommand,
  bindingCodeExpiryLabel,
  connectionAgentChoices,
  connectionAgentRuntimeOptions,
  groupBindingAgentOptions,
  groupBindingChannelAvailability,
  prepareProviderConnectWindow,
} from "@/components/projects/private-work/project-connections-page";
import type { ProjectAssetItem, ProjectAssetList } from "@/core/shared-assets";

const AGENT: ProjectAssetItem = {
  id: "22222222-2222-4222-8222-222222222222",
  scope: "project",
  project_id: "11111111-1111-4111-8111-111111111111",
  slug: "analyst",
  display_name: "Analyst",
  status: "active",
  current_published_version_id: "33333333-3333-4333-8333-333333333333",
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-27T00:00:00Z",
  updated_at: "2026-07-27T00:00:00Z",
  capabilities: ["shared_assets.read", "shared_assets.execute"],
  binding: null,
};

describe("Project Connections Agent runtime gate", () => {
  test("builds a persistent binding command and readable expiry", () => {
    expect(bindingCodeCommand("bind-code")).toBe("/connect bind-code");
    expect(bindingCodeExpiryLabel(600)).toBe("10 分钟内有效");
    expect(bindingCodeExpiryLabel(61)).toBe("2 分钟内有效");
    expect(bindingCodeExpiryLabel(30)).toBe("30 秒后失效");
    expect(bindingCodeExpiryLabel(0)).toBe("连接码已失效");
  });

  test("does not open a temporary page for binding-code providers", () => {
    let prepareCalls = 0;
    const result = prepareProviderConnectWindow("binding_code", () => {
      prepareCalls += 1;
      return {} as Window;
    });

    expect(result).toBeNull();
    expect(prepareCalls).toBe(0);
  });

  test("prepares a page only for providers that return a deep link", () => {
    const popup = {} as Window;
    let prepareCalls = 0;
    const result = prepareProviderConnectWindow("deep_link", () => {
      prepareCalls += 1;
      return popup;
    });

    expect(result).toBe(popup);
    expect(prepareCalls).toBe(1);
  });

  test("offers an unbound Main Agent without binding authority", () => {
    const main: ProjectAssetItem = {
      ...AGENT,
      id: "55555555-5555-4555-8555-555555555555",
      scope: "system",
      project_id: null,
      slug: "project-assistant",
      display_name: "Main",
      capabilities: ["shared_assets.read", "shared_assets.execute"],
    };
    const catalog: ProjectAssetList = {
      project_items: [],
      system_items: [main],
      request_id: "req-main",
    };

    expect(connectionAgentChoices(catalog, [])).toEqual([main]);
  });

  test("does not duplicate an already executable Main Agent", () => {
    const main: ProjectAssetItem = {
      ...AGENT,
      id: "55555555-5555-4555-8555-555555555555",
      scope: "system",
      project_id: null,
      slug: "project-assistant",
      display_name: "Main",
      binding: {
        project_id: "11111111-1111-4111-8111-111111111111",
        kind: "agent",
        asset_id: "55555555-5555-4555-8555-555555555555",
        version_id: "33333333-3333-4333-8333-333333333333",
        enabled: true,
        version: 1,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: "2026-07-27T00:00:00Z",
        updated_at: "2026-07-27T00:00:00Z",
      },
    };
    const catalog: ProjectAssetList = {
      project_items: [],
      system_items: [main],
      request_id: "req-main-bound",
    };

    expect(connectionAgentChoices(catalog, [main])).toEqual([main]);
  });

  test("offers only MCP-verified Agents and keeps blocked Agents explicit", () => {
    const blocked = {
      ...AGENT,
      id: "44444444-4444-4444-8444-444444444444",
      display_name: "Legacy MCP Agent",
    };
    const result = connectionAgentRuntimeOptions(
      [AGENT, blocked],
      [
        { status: "ready", reason: null },
        {
          status: "blocked",
          reason: "该 MCP 版本当前不能作为 Agent 依赖",
        },
      ],
    );

    expect(result.readyAgents.map(({ id }) => id)).toEqual([AGENT.id]);
    expect(result.blockedAgents.map(({ id }) => id)).toEqual([blocked.id]);
  });

  test("fails closed while dependency verification is still loading", () => {
    const result = connectionAgentRuntimeOptions(
      [AGENT],
      [{ status: "loading", reason: "正在验证 Agent 的 MCP 依赖，请稍候。" }],
    );
    expect(result.readyAgents).toEqual([]);
    expect(result.blockedAgents).toEqual([]);
  });

  test("allows new group bindings only while the Feishu instance is running", () => {
    expect(
      groupBindingChannelAvailability({
        provider: "feishu",
        configured: true,
        enabled: true,
        status: "running",
      }),
    ).toEqual({ enabled: true, reason: null });
    expect(
      groupBindingChannelAvailability({
        provider: "feishu",
        configured: false,
        enabled: false,
        status: "unconfigured",
      }),
    ).toEqual({ enabled: false, reason: "请先配置飞书渠道。" });
    expect(
      groupBindingChannelAvailability({
        provider: "feishu",
        configured: true,
        enabled: false,
        status: "disabled",
      }),
    ).toEqual({ enabled: false, reason: "请先启用飞书渠道。" });
    expect(
      groupBindingChannelAvailability({
        provider: "feishu",
        configured: true,
        enabled: true,
        status: "error",
      }),
    ).toEqual({ enabled: false, reason: "飞书渠道未运行，暂时无法绑定群聊。" });
  });

  test("reuses the page Agent gate for group binding choices", () => {
    const blocked = {
      ...AGENT,
      id: "44444444-4444-4444-8444-444444444444",
      display_name: "Unavailable Agent",
    };

    expect(
      groupBindingAgentOptions(
        [AGENT],
        [{ agent: blocked, reason: "MCP 依赖不可用" }],
      ),
    ).toEqual([
      {
        id: AGENT.id,
        scope: AGENT.scope,
        displayName: AGENT.display_name,
        available: true,
        unavailableReason: null,
      },
      {
        id: blocked.id,
        scope: blocked.scope,
        displayName: blocked.display_name,
        available: false,
        unavailableReason: "MCP 依赖不可用",
      },
    ]);
  });
});
