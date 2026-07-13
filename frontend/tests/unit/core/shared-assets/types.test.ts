import { describe, expect, test } from "@rstest/core";

import { CAPABILITIES, capabilitySchema } from "@/core/projects/types";
import {
  agentVersionInputSchema,
  agentVersionSchema,
  assetSummarySchema,
  createCredentialInputSchema,
  credentialVersionSchema,
  credentialMetadataSchema,
  mcpVersionInputSchema,
  skillVersionInputSchema,
  systemBindingSchema,
  versionHistoryResponseSchema,
  versionResponseSchema,
} from "@/core/shared-assets/types";

const ASSET_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";
const PROJECT_ID = "33333333-3333-4333-8333-333333333333";

describe("shared asset contracts", () => {
  test("strictly parses the Gateway asset item without inventing endpoint metadata", () => {
    const asset = assetSummarySchema.parse({
      id: ASSET_ID,
      scope: "project",
      project_id: PROJECT_ID,
      slug: "writer",
      display_name: "Writer",
      status: "active",
      current_published_version_id: VERSION_ID,
      version: 1,
      created_by_user_id: "user-1",
      created_at: "2026-07-14T00:00:00Z",
      updated_at: "2026-07-14T00:00:00Z",
    });

    expect(asset).not.toHaveProperty("capabilities");
    expect(asset).not.toHaveProperty("kind");
    expect(() =>
      assetSummarySchema.parse({ ...asset, role: "admin" }),
    ).toThrow();
  });

  test.each([
    "plaintext",
    "ciphertext",
    "nonce",
    "key_id",
    "storage_locator",
    "secret_hash",
  ])("rejects credential secret storage field %s", (field) => {
    expect(() =>
      credentialMetadataSchema.parse({
        id: ASSET_ID,
        scope: "project",
        project_id: PROJECT_ID,
        name: "github",
        display_name: "GitHub",
        credential_type: "token",
        status: "active",
        current_version_id: VERSION_ID,
        version: 1,
        created_by_user_id: "user-1",
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
        [field]: "forbidden",
      }),
    ).toThrow();
  });

  test("strictly parses discriminated Gateway version envelopes", () => {
    const agentVersion = agentVersionSchema.parse({
      id: VERSION_ID,
      agent_id: ASSET_ID,
      version_number: 1,
      workflow_status: "published",
      description: "Review changes",
      soul: "Be precise",
      model_ref: "default",
      tool_groups: ["web"],
      skill_version_ids: [],
      mcp_version_ids: [],
      supersedes_version_id: null,
      payload_checksum: "a".repeat(64),
      created_by_user_id: "user-1",
      created_at: "2026-07-14T00:00:00Z",
    });
    expect(
      versionResponseSchema.parse({ data: agentVersion, request_id: "req-3" }),
    ).toMatchObject({ data: { agent_id: ASSET_ID } });
    expect(
      versionHistoryResponseSchema.parse({
        data: [agentVersion],
        request_id: "req-3",
      }),
    ).toMatchObject({ data: [{ version_number: 1 }] });

    expect(() =>
      credentialVersionSchema.parse({
        id: VERSION_ID,
        credential_id: ASSET_ID,
        version_number: 1,
        status: "active",
        payload_schema_version: 1,
        payload_schema: { env: ["TOKEN"] },
        supersedes_version_id: null,
        created_by_user_id: "user-1",
        created_at: "2026-07-14T00:00:00Z",
        ciphertext: "forbidden",
      }),
    ).toThrow();
  });

  test("keeps Agent Skill MCP authoring payloads distinct and credential secrets write-only", () => {
    expect(
      agentVersionInputSchema.parse({
        description: "Writer",
        soul: "Be precise",
        model_ref: "default",
        tool_groups: [],
        skill_version_ids: [],
        mcp_version_ids: [],
        expected_asset_version: 1,
      }),
    ).toMatchObject({ soul: "Be precise" });
    expect(
      skillVersionInputSchema.parse({
        files: [
          {
            path: "SKILL.md",
            content_base64: "IyBTa2lsbA==",
            media_type: "text/markdown",
          },
        ],
        expected_asset_version: 1,
      }),
    ).toMatchObject({ files: [{ path: "SKILL.md" }] });
    expect(
      mcpVersionInputSchema.parse({
        description: "GitHub",
        transport: "stdio",
        command: "github-mcp",
        args: [],
        env: {},
        headers: {},
        oauth: {},
        routing: {},
        tool_overrides: {},
        timeout_seconds: 30,
        credential_slots: [],
        expected_asset_version: 1,
      }),
    ).toMatchObject({ transport: "stdio" });
    expect(() =>
      agentVersionInputSchema.parse({
        files: [],
        expected_asset_version: 1,
      }),
    ).toThrow();

    const input = createCredentialInputSchema.parse({
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    });
    expect(input.payload.env?.TOKEN).toBe("write-only");
    expect(input).not.toHaveProperty("ciphertext");
  });

  test("strictly parses system binding metadata", () => {
    expect(
      systemBindingSchema.parse({
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: ASSET_ID,
        version_id: VERSION_ID,
        enabled: true,
        version: 1,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
        request_id: "req-4",
      }),
    ).toMatchObject({ enabled: true, version: 1 });
  });

  test("declares the Gateway binding capability without deriving it from role", () => {
    expect(CAPABILITIES).toContain("shared_assets.manage_bindings");
    expect(capabilitySchema.parse("shared_assets.manage_bindings")).toBe(
      "shared_assets.manage_bindings",
    );
  });
});
