import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { McpAssetDetail } from "@/components/projects/assets/mcp-asset-detail";
import type { AssetVersion } from "@/core/shared-assets";

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

describe("MCP asset detail", () => {
  test("shows the safe definition, slots and grants without fabricating runtime health", () => {
    const html = renderToStaticMarkup(<McpAssetDetail version={version} />);

    for (const text of [
      "GitHub repository access",
      "streamable_http",
      "https://mcp.example.test",
      "45 秒",
      "MODE",
      "readonly",
      "github-token",
      "Authorization",
      "已授权",
    ]) {
      expect(html).toContain(text);
    }
    for (const unsupported of ["在线", "连接正常", "工具数量", "测试连接"]) {
      expect(html).not.toContain(unsupported);
    }
  });
});
