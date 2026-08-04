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
  url: string | null = "http://localhost:8771/api/mcp",
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
    "http://localhost:8771/api/mcp",
    "http://LocalHost:8771/api/mcp",
    "https://127.0.0.1:9443/mcp",
    "http://10.20.30.40/mcp",
    "http://[::1]:8771/api/mcp",
    "https://[fd00::1234]/mcp",
  ])("accepts a Project HTTP(S) URL on localhost or a canonical IP: %s", (url) => {
    expect(
      mcpVersionRuntimeBlockReason(mcpVersion("http", url), "project"),
    ).toBeNull();
  });

  test.each([
    "https://user:password@127.0.0.1",
    "https://127.0.0.1/path?token=opaque",
    "https://127.0.0.1/path?",
    "https://127.0.0.1/path#fragment",
    "https://127.0.0.1/path#",
    "ftp://127.0.0.1/mcp",
    "http://localhost:0/mcp",
    "http://*/mcp",
    "http://trans-resource:8771/api/mcp",
    "https://mcp.internal/api/mcp",
    "https://mcp.example.com/api/mcp",
    "http://localhost./api/mcp",
    "http://foo.localhost/api/mcp",
    "http://127.1/api/mcp",
    "http://0x7f.0.0.1/api/mcp",
    "http://0177.0.0.1/api/mcp",
    "http://[0:0:0:0:0:0:0:1]/api/mcp",
    "http://[FD00::1234]/api/mcp",
    "not-a-url",
  ])("blocks an invalid Project remote URL: %s", (url) => {
    const reason = mcpVersionRuntimeBlockReason(
      mcpVersion("http", url),
      "project",
    );
    expect(reason).toContain("HTTP 或 HTTPS URL");
    expect(reason).toContain("管理员配置的允许网段");
    expect(reason).not.toContain("完全一致");
    expect(reason).not.toContain("解析出的目标 IP");
  });

  test.each(["stdio", "streamable_http"] as const)(
    "keeps historical %s readable but marks it non-runnable",
    (transport) => {
      expect(
        mcpVersionRuntimeBlockReason(mcpVersion(transport), "project"),
      ).toContain("此历史配置可以查看，但不能发布、绑定或用于 Agent");
    },
  );

  test("accepts query Credential slots and blocks unsupported Project groups", () => {
    const querySlotVersion = mcpVersion("http");
    querySlotVersion.definition.credential_slots = [
      {
        name: "api-key",
        purpose: "Remote query authentication",
        payload_schema: { query: ["key"] },
        required: true,
      },
    ];
    expect(
      mcpVersionRuntimeBlockReason(querySlotVersion, "project"),
    ).toBeNull();

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
      "仅支持 headers 或 query",
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
    ).toContain("仅支持 headers 或 query");

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
    ).toContain("仅支持 headers 或 query");
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
