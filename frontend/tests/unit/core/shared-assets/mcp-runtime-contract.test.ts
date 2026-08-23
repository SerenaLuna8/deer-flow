import { describe, expect, test } from "@rstest/core";

import {
  mcpVersionRuntimeBlockReason,
  supportedMcpVersionIds,
} from "@/core/shared-assets/mcp-runtime";

type RuntimeVersion = Parameters<typeof mcpVersionRuntimeBlockReason>[0];
type ScopedVersion = Parameters<typeof supportedMcpVersionIds>[0][number];

function projectMcpVersion(
  payloadSchema: Record<string, string[]>,
  id = "00000000-0000-4000-8000-000000000001",
): RuntimeVersion {
  const slot = {
    name: "auth",
    purpose: "MCP request credentials",
    payload_schema: payloadSchema,
    required: true,
  };
  return {
    definition: {
      description: "Combined credentials",
      transport: "http",
      command: null,
      args: [],
      url: "http://127.0.0.1:65535/mcp",
      env: {},
      headers: {},
      oauth: {},
      routing: {},
      tool_overrides: {},
      timeout_seconds: 30,
      secret_slots: [slot],
    },
    secret_slots: [{ id, ...slot }],
  };
}

describe("Project MCP runtime eligibility", () => {
  test("allows Header and Query fields in one secret slot", () => {
    const version = projectMcpVersion({
      headers: ["Authorization"],
      query: ["api_key"],
    });

    expect(mcpVersionRuntimeBlockReason(version, "project")).toBeNull();
    expect(
      supportedMcpVersionIds([
        {
          scope: "project",
          version: {
            id: "00000000-0000-4000-8000-000000000001",
            ...version,
          } as ScopedVersion["version"],
        },
      ]),
    ).toContain("00000000-0000-4000-8000-000000000001");
  });

  test.each([
    ["an empty slot", {}],
    ["an empty Header group", { headers: [] }],
    [
      "an empty Query group beside a populated Header group",
      {
        headers: ["Authorization"],
        query: [],
      },
    ],
    ["an env group", { env: ["API_KEY"] }],
    ["an oauth group", { oauth: ["token"] }],
    ["a forbidden Header name", { headers: ["Host"] }],
    [
      "case-insensitive duplicate Header names",
      {
        headers: ["Authorization", "authorization"],
      },
    ],
    ["an invalid Query name", { query: ["api key"] }],
    ["duplicate Query names", { query: ["api_key", "api_key"] }],
  ])("rejects %s", (_label, payloadSchema) => {
    expect(
      mcpVersionRuntimeBlockReason(projectMcpVersion(payloadSchema), "project"),
    ).not.toBeNull();
  });
});
