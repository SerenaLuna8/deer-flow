import { describe, expect, test } from "@rstest/core";

import { createConfiguredMcpInputSchema } from "@/core/shared-assets";

function configuredMcpInput(payloadSchema: Record<string, string[]>) {
  return {
    display_name: "Combined credentials",
    slug: "combined-credentials",
    url: "http://127.0.0.1:65535/mcp",
    secret_slots: [
      {
        name: "auth",
        purpose: "MCP request credentials",
        payload_schema: payloadSchema,
        required: true,
      },
    ],
  };
}

describe("Project MCP credential input contract", () => {
  test("accepts Header and Query fields in one atomic slot", () => {
    expect(
      createConfiguredMcpInputSchema.safeParse(
        configuredMcpInput({
          headers: ["Authorization"],
          query: ["api_key"],
        }),
      ).success,
    ).toBe(true);
  });

  test.each([
    ["an empty slot", {}],
    ["an unsupported section", { env: ["API_KEY"] }],
  ])("rejects %s", (_label, payloadSchema) => {
    expect(
      createConfiguredMcpInputSchema.safeParse(
        configuredMcpInput(payloadSchema),
      ).success,
    ).toBe(false);
  });
});
