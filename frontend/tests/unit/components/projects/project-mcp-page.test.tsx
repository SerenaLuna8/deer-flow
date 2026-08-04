import { describe, expect, test } from "@rstest/core";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  McpAssetDetail,
  McpToolInventorySection,
} from "@/components/projects/assets/mcp-asset-detail";
import { projectMcpCanTestService } from "@/components/projects/assets/project-mcp-page";
import { I18nProvider } from "@/core/i18n/context";
import {
  SharedAssetApiError,
  type AssetVersion,
  type McpToolInventory,
} from "@/core/shared-assets";

const SLOT_ID = "33333333-3333-4333-8333-333333333333";
const version: Extract<AssetVersion, { mcp_server_id: string }> = {
  id: "11111111-1111-4111-8111-111111111111",
  mcp_server_id: "22222222-2222-4222-8222-222222222222",
  version_number: 3,
  workflow_status: "published",
  definition: {
    description: "GitHub repository access",
    transport: "streamable_http",
    command: null,
    args: [],
    url: "https://mcp.example.test",
    env: { MODE: "readonly" },
    headers: { "X-Client": "deer-flow" },
    oauth: { auth_mode: "oauth2" },
    routing: { region: "global" },
    tool_overrides: { disabled: ["delete_repository"] },
    timeout_seconds: 45,
    credential_slots: [
      {
        name: "github-token",
        purpose: "GitHub access token",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
    ],
  },
  credential_slots: [
    {
      id: SLOT_ID,
      name: "github-token",
      purpose: "GitHub access token",
      payload_schema: { headers: ["Authorization"] },
      required: true,
    },
  ],
  credential_grants: [
    {
      id: "44444444-4444-4444-8444-444444444444",
      mcp_server_version_id: "11111111-1111-4111-8111-111111111111",
      credential_slot_id: SLOT_ID,
      credential_version_id: "55555555-5555-4555-8555-555555555555",
      status: "active",
      version: 1,
      created_by_user_id: "admin-1",
      created_at: "2026-07-21T00:00:00Z",
    },
  ],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  submitted_at: "2026-07-21T00:00:00Z",
  reviewed_at: "2026-07-21T00:05:00Z",
  reviewed_by_user_id: "admin-1",
  created_by_user_id: "editor-1",
  created_at: "2026-07-21T00:00:00Z",
};

const inventory: McpToolInventory = {
  status: "ready",
  tools: [
    {
      name: "maps_direction_driving",
      description: "根据起终点经纬度坐标规划驾车通勤方案",
    },
    {
      name: "maps_weather",
      description: "根据城市名称查询指定城市的天气",
    },
  ],
  last_attempt_at: "2026-07-21T00:06:00Z",
  last_success_at: "2026-07-21T00:06:00Z",
  error_code: null,
};

function renderMcp(node: ReactNode, locale: "zh-CN" | "en-US" = "zh-CN") {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>{node}</I18nProvider>,
  );
}

describe("MCP asset detail", () => {
  test("keeps connection fields on one row and omits Credential slot internals", () => {
    const html = renderMcp(
      <McpAssetDetail
        version={version}
        scope="project"
        toolInventory={inventory}
      />,
    );

    for (const text of [
      "GitHub repository access",
      "streamable_http",
      "https://mcp.example.test",
      "45 秒",
      "MODE",
      "readonly",
      "服务工具",
      "2 个工具",
      "maps_direction_driving",
      "根据城市名称查询指定城市的天气",
      "此历史配置可以查看，但不能发布、绑定或用于 Agent",
    ]) {
      expect(html).toContain(text);
    }
    for (const unsupported of ["在线", "连接正常", "测试连接"]) {
      expect(html).not.toContain(unsupported);
    }
    expect(html).toContain(
      "sm:grid-cols-[minmax(0,0.75fr)_minmax(0,0.75fr)_minmax(0,1.5fr)]",
    );
    expect(html).not.toContain("Credential 槽位");
    expect(html).not.toContain("github-token");
    expect(html).not.toContain("Authorization");
    expect(html).not.toContain("已绑定");
    expect(html).not.toContain("版本");
  });

  test("distinguishes never discovered, failed, stale and empty inventories", () => {
    const never = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{
          status: "never_discovered",
          tools: [],
          last_attempt_at: null,
          last_success_at: null,
          error_code: null,
        }}
      />,
    );
    expect(never).toContain("尚无工具发现记录");
    expect(never).toContain("测试服务");

    const failed = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{
          status: "failed",
          tools: [],
          last_attempt_at: "2026-07-21T00:06:00Z",
          last_success_at: null,
          error_code: "mcp_discovery_unavailable",
        }}
      />,
    );
    expect(failed).toContain("最近一次连接 MCP 服务失败");
    expect(failed).not.toContain("https://");

    const stale = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{
          status: "stale",
          tools: [],
          last_attempt_at: "2026-07-21T00:06:00Z",
          last_success_at: "2026-07-20T00:06:00Z",
          error_code: null,
        }}
      />,
    );
    expect(stale).toContain("配置或 Credential 授权已变化");

    const empty = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{
          ...inventory,
          tools: [],
        }}
      />,
    );
    expect(empty).toContain("未提供可用工具");

    const degraded = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{
          ...inventory,
          status: "degraded",
          error_code: "mcp_discovery_unavailable",
        }}
      />,
    );
    expect(degraded).toContain("当前展示上次成功发现的工具");
    expect(degraded).toContain("maps_weather");

    const draft = renderMcp(<McpToolInventorySection workflowStatus="draft" />);
    expect(draft).toContain("此配置尚未生效");
    expect(draft).not.toContain("审批");
  });

  test("explains loading and request failures and disables duplicate service tests", () => {
    const loading = renderMcp(
      <McpToolInventorySection workflowStatus="published" isLoading />,
    );
    expect(loading).toContain('aria-label="正在加载工具目录"');

    const forbidden = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        error={
          new SharedAssetApiError(
            403,
            "ASSET_FORBIDDEN",
            "private upstream detail",
          )
        }
        toolDiscoveryPending
        onTest={() => undefined}
      />,
    );
    expect(forbidden).toContain("没有查看此 MCP 工具目录的权限");
    expect(forbidden).toContain("测试中…");
    expect(forbidden).toContain("disabled");
    expect(forbidden).not.toContain("private upstream detail");
  });

  test("keeps the previous tools visible while discovery is testing", () => {
    const html = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{ ...inventory, status: "testing", error_code: null }}
        onTest={() => undefined}
      />,
    );

    expect(html).toContain("正在测试服务并读取工具…");
    expect(html).toContain("maps_weather");
    expect(html).toContain("2 个工具");
    expect(html).toContain("测试中…");
    expect(html).toContain("disabled");
  });

  test("offers a real service test and separates its failure from saved configuration", () => {
    const initial = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{
          status: "never_discovered",
          tools: [],
          last_attempt_at: null,
          last_success_at: null,
          error_code: null,
        }}
        onTest={() => undefined}
      />,
    );
    expect(initial).toContain("测试服务");
    expect(initial).not.toContain("重新测试");

    const failed = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={inventory}
        toolDiscoveryError={
          new SharedAssetApiError(
            503,
            "ASSET_STORAGE_UNAVAILABLE",
            "private discovery detail",
          )
        }
        onTest={() => undefined}
      />,
    );
    expect(failed).toContain("配置已保存，但测试服务失败");
    expect(failed).toContain("重新测试");
    expect(failed).toContain("maps_weather");
    expect(failed).not.toContain("private discovery detail");
    expect(failed).not.toContain("保存失败");
  });

  test("renders service discovery controls in English", () => {
    const html = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={{
          status: "never_discovered",
          tools: [],
          last_attempt_at: null,
          last_success_at: null,
          error_code: null,
        }}
        toolDiscoveryPending
        onTest={() => undefined}
      />,
      "en-US",
    );

    expect(html).toContain("Testing the service and reading tools…");
    expect(html).toContain("Testing…");
    expect(html).not.toContain("正在测试");
  });

  test("shows service testing only for editable executable project MCPs", () => {
    const denied = [
      {
        scope: "system" as const,
        capabilities: ["shared_assets.edit", "shared_assets.execute"],
      },
      {
        scope: "project" as const,
        capabilities: ["shared_assets.execute"],
      },
      {
        scope: "project" as const,
        capabilities: ["shared_assets.edit"],
      },
    ];
    for (const item of denied) {
      expect(projectMcpCanTestService(item as never)).toBe(false);
      const html = renderMcp(
        <McpToolInventorySection
          workflowStatus="published"
          inventory={inventory}
          onTest={
            projectMcpCanTestService(item as never)
              ? () => undefined
              : undefined
          }
        />,
      );
      expect(html).not.toContain('aria-label="重新测试"');
    }

    const allowed = {
      scope: "project" as const,
      capabilities: ["shared_assets.edit", "shared_assets.execute"],
    };
    expect(projectMcpCanTestService(allowed as never)).toBe(true);
    const html = renderMcp(
      <McpToolInventorySection
        workflowStatus="published"
        inventory={inventory}
        onTest={
          projectMcpCanTestService(allowed as never)
            ? () => undefined
            : undefined
        }
      />,
    );
    expect(html).toContain('aria-label="重新测试"');
  });
});
