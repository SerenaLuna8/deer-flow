import { describe, expect, test } from "@rstest/core";

import type { AssetVersion } from "@/core/shared-assets";
import {
  mcpDependencyRuntimeBlockReason,
  mcpVersionRuntimeBlockReason,
  supportedMcpVersionIds,
  type ScopedMcpVersion,
} from "@/core/shared-assets/mcp-runtime";

const VERSION_ID = "11111111-1111-4111-8111-111111111111";
const ASSET_ID = "22222222-2222-4222-8222-222222222222";

function mcpVersion(
  transport: "stdio" | "sse" | "http" | "streamable_http",
  url: string | null = "https://mcp.example.test",
): Extract<AssetVersion, { mcp_server_id: string }> {
  return {
    id: VERSION_ID,
    mcp_server_id: ASSET_ID,
    version_number: 1,
    workflow_status: "published",
    definition: {
      description: "MCP",
      transport,
      command: transport === "stdio" ? "server" : null,
      args: [],
      url,
      env: {},
      headers: {},
      oauth: {},
      routing: {},
      tool_overrides: {},
      timeout_seconds: 30,
      credential_slots: [],
    },
    credential_slots: [],
    credential_grants: [],
    supersedes_version_id: null,
    payload_checksum: "a".repeat(64),
    submitted_at: null,
    reviewed_at: null,
    reviewed_by_user_id: null,
    created_by_user_id: "user-1",
    created_at: "2026-07-27T00:00:00Z",
  };
}

function scoped(
  scope: "project" | "system",
  version: ReturnType<typeof mcpVersion>,
): ScopedMcpVersion {
  return { scope, version };
}

describe("MCP runtime compatibility", () => {
  test.each(["sse", "http"] as const)(
    "accepts %s only when a remote URL is present",
    (transport) => {
      expect(
        mcpVersionRuntimeBlockReason(mcpVersion(transport), "project"),
      ).toBeNull();
      expect(
        mcpVersionRuntimeBlockReason(mcpVersion(transport, null), "project"),
      ).toContain("缺少 URL");
    },
  );

  test.each([
    "http://mcp.example.test",
    "https://user:password@mcp.example.test",
    "https://mcp.example.test/path?token=opaque",
    "https://mcp.example.test/path#fragment",
    "https://localhost/mcp",
    "https://0x7f.0.0.1/mcp",
    "not-a-url",
  ])("blocks an invalid Project remote URL: %s", (url) => {
    expect(
      mcpVersionRuntimeBlockReason(mcpVersion("http", url), "project"),
    ).toContain("HTTPS URL");
  });

  test.each(["stdio", "streamable_http"] as const)(
    "keeps historical %s readable but marks it non-runnable",
    (transport) => {
      expect(
        mcpVersionRuntimeBlockReason(mcpVersion(transport), "project"),
      ).toContain("此历史版本可以查看，但不能发布、绑定或用于 Agent");
    },
  );

  test("blocks historical remote definitions with OAuth or non-header Credential slots", () => {
    const oauthVersion = mcpVersion("http");
    oauthVersion.definition.oauth = { client_id: "public-id" };
    expect(mcpVersionRuntimeBlockReason(oauthVersion, "project")).toContain(
      "OAuth",
    );

    const envSlotVersion = mcpVersion("sse");
    envSlotVersion.definition.credential_slots = [
      {
        name: "api-token",
        purpose: "Legacy env injection",
        payload_schema: { env: ["TOKEN"] },
        required: true,
      },
    ];
    expect(mcpVersionRuntimeBlockReason(envSlotVersion, "project")).toContain(
      "仅支持 headers",
    );

    const customSlotVersion = mcpVersion("sse");
    customSlotVersion.definition.credential_slots = [
      {
        name: "api-token",
        purpose: "Unknown injection",
        payload_schema: { custom: ["TOKEN"] },
        required: true,
      },
    ];
    expect(
      mcpVersionRuntimeBlockReason(customSlotVersion, "project"),
    ).toContain("仅支持 headers");

    const persistedSlotVersion = mcpVersion("http");
    persistedSlotVersion.credential_slots = [
      {
        id: "55555555-5555-4555-8555-555555555555",
        name: "api-token",
        purpose: "Persisted legacy OAuth slot",
        payload_schema: { oauth: ["access_token"] },
        required: true,
      },
    ];
    expect(
      mcpVersionRuntimeBlockReason(persistedSlotVersion, "project"),
    ).toContain("仅支持 headers");
  });

  test.each(["Authorization", "authorization"])(
    "blocks duplicate Project Credential headers across slots: %s",
    (duplicateName) => {
      const duplicateHeaderVersion = mcpVersion("http");
      duplicateHeaderVersion.credential_slots = [
        {
          id: "55555555-5555-4555-8555-555555555555",
          name: "primary",
          purpose: "Primary credential",
          payload_schema: { headers: ["Authorization"] },
          required: true,
        },
        {
          id: "66666666-6666-4666-8666-666666666666",
          name: "secondary",
          purpose: "Secondary credential",
          payload_schema: { headers: [duplicateName] },
          required: true,
        },
      ];

      expect(
        mcpVersionRuntimeBlockReason(duplicateHeaderVersion, "project"),
      ).toContain("不能重复声明请求头");
    },
  );

  test("does not treat mirrored definition and persisted slots as duplicates", () => {
    const mirroredVersion = mcpVersion("http");
    mirroredVersion.definition.credential_slots = [
      {
        name: "api-token",
        purpose: "Definition mirror",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
    ];
    mirroredVersion.credential_slots = [
      {
        id: "55555555-5555-4555-8555-555555555555",
        name: "api-token",
        purpose: "Persisted slot",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
    ];

    expect(
      mcpVersionRuntimeBlockReason(mirroredVersion, "project"),
    ).toBeNull();
  });

  test("preserves supported system stdio, OAuth, env, and header capabilities", () => {
    const stdio = mcpVersion("stdio", null);
    stdio.definition.env = { MODE: "catalog" };
    stdio.definition.credential_slots = [
      {
        name: "api-token",
        purpose: "Runtime env",
        payload_schema: { env: ["TOKEN"] },
        required: true,
      },
    ];
    expect(mcpVersionRuntimeBlockReason(stdio, "system")).toBeNull();

    const remote = mcpVersion("http");
    remote.definition.oauth = {
      enabled: true,
      token_url: "https://identity.example.test/token",
    };
    remote.credential_slots = [
      {
        id: "55555555-5555-4555-8555-555555555555",
        name: "oauth-token",
        purpose: "OAuth material",
        payload_schema: {
          headers: ["X-Tenant"],
          oauth: ["client_secret"],
        },
        required: true,
      },
    ];
    expect(mcpVersionRuntimeBlockReason(remote, "system")).toBeNull();
  });

  test("blocks only unsupported or malformed system runtime definitions", () => {
    expect(
      mcpVersionRuntimeBlockReason(mcpVersion("streamable_http"), "system"),
    ).toContain("Private runtime");

    const missingCommand = mcpVersion("stdio", null);
    missingCommand.definition.command = null;
    expect(mcpVersionRuntimeBlockReason(missingCommand, "system")).toContain(
      "command",
    );

    const invalidStdioSlot = mcpVersion("stdio", null);
    invalidStdioSlot.definition.credential_slots = [
      {
        name: "api-token",
        purpose: "Wrong transport section",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
    ];
    expect(mcpVersionRuntimeBlockReason(invalidStdioSlot, "system")).toContain(
      "仅支持 env",
    );
  });

  test("only returns supported MCP IDs and blocks missing Agent dependencies", () => {
    const supported = mcpVersion("http");
    const unsupported = {
      ...mcpVersion("stdio"),
      id: "33333333-3333-4333-8333-333333333333",
    };
    const versions = [
      scoped("project", supported),
      scoped("project", unsupported),
    ];
    const ids = supportedMcpVersionIds(versions);

    expect([...ids]).toEqual([supported.id]);
    expect(
      mcpDependencyRuntimeBlockReason([supported.id], versions),
    ).toBeNull();
    expect(
      mcpDependencyRuntimeBlockReason([unsupported.id], versions),
    ).toContain("不能作为 Agent 依赖");
    expect(
      mcpDependencyRuntimeBlockReason(
        ["44444444-4444-4444-8444-444444444444"],
        versions,
      ),
    ).toContain("无法确认");
  });

  test("evaluates mixed Agent dependencies with each asset's own scope", () => {
    const systemStdio = mcpVersion("stdio", null);
    const projectStdio = {
      ...mcpVersion("stdio", null),
      id: "33333333-3333-4333-8333-333333333333",
    };
    expect(
      mcpDependencyRuntimeBlockReason(
        [systemStdio.id],
        [scoped("system", systemStdio)],
      ),
    ).toBeNull();
    expect(
      mcpDependencyRuntimeBlockReason(
        [projectStdio.id],
        [scoped("project", projectStdio)],
      ),
    ).toContain("不能作为 Agent 依赖");
  });
});
