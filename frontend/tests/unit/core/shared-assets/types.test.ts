import { describe, expect, test } from "@rstest/core";

import { CAPABILITIES, capabilitySchema } from "@/core/projects/types";
import {
  agentInstructionsInputSchema,
  agentVersionSchema,
  assetSummarySchema,
  configuredMcpResponseSchema,
  createConfiguredMcpInputSchema,
  createCredentialInputSchema,
  credentialPayloadSchema,
  credentialVersionSchema,
  credentialMetadataSchema,
  deleteCredentialInputSchema,
  mcpToolInventoryResponseSchema,
  mcpToolDiscoveryAttemptResponseSchema,
  mcpVersionSchema,
  mcpVersionInputSchema,
  projectAssetListSchema,
  projectCredentialListSchema,
  projectDefaultAgentInputSchema,
  projectDefaultAgentSchema,
  projectMcpEditableConfigurationResponseSchema,
  skillFileForkInputSchema,
  skillVersionFileContentResponseSchema,
  skillVersionInputSchema,
  skillVersionSchema,
  systemBindingSchema,
  updateConfiguredMcpInputSchema,
  versionHistoryResponseSchema,
  versionResponseSchema,
} from "@/core/shared-assets/types";

const ASSET_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";
const PROJECT_ID = "33333333-3333-4333-8333-333333333333";

describe("shared asset contracts", () => {
  test("strictly accepts only a safe flat project MCP configured-create payload", () => {
    const input = {
      slug: "amap-mcp",
      display_name: "高德地图 MCP",
      description: "地图与路线规划",
      transport: "http" as const,
      command: null,
      args: [],
      url: "http://localhost:8771/api/mcp",
      env: {},
      headers: {},
      oauth: {},
      routing: {},
      tool_overrides: {},
      timeout_seconds: 30,
      credential_slots: [
        {
          name: "api-key",
          purpose: "高德地图 API Key",
          payload_schema: { query: ["key"] },
          required: true,
        },
      ],
    };

    expect(createConfiguredMcpInputSchema.parse(input)).toEqual(input);
    expect(
      createConfiguredMcpInputSchema.safeParse({
        ...input,
        expected_asset_version: 1,
      }).success,
    ).toBe(false);
    expect(
      createConfiguredMcpInputSchema.safeParse({
        ...input,
        transport: "stdio",
        command: "mcp-amap",
        url: null,
      }).success,
    ).toBe(false);
    expect(
      createConfiguredMcpInputSchema.safeParse({
        ...input,
        url: "https://mcp.amap.com/mcp?key=must-not-be-sent",
      }).success,
    ).toBe(false);

    const definition = {
      description: input.description,
      transport: input.transport,
      command: input.command,
      args: input.args,
      url: input.url,
      env: input.env,
      headers: input.headers,
      oauth: input.oauth,
      routing: input.routing,
      tool_overrides: input.tool_overrides,
      timeout_seconds: input.timeout_seconds,
      credential_slots: input.credential_slots,
    };
    const update = { ...definition, expected_asset_version: 4 };
    expect(updateConfiguredMcpInputSchema.parse(update)).toEqual(update);
    expect(updateConfiguredMcpInputSchema.safeParse(definition).success).toBe(
      false,
    );
    expect(
      updateConfiguredMcpInputSchema.safeParse({
        ...update,
        transport: "stdio",
      }).success,
    ).toBe(false);
    expect(
      updateConfiguredMcpInputSchema.safeParse({
        ...update,
        url: "https://mcp.amap.com/mcp?key=must-not-be-sent",
      }).success,
    ).toBe(false);
    expect(
      updateConfiguredMcpInputSchema.safeParse({
        ...update,
        credential_slots: [
          update.credential_slots[0],
          update.credential_slots[0],
        ],
      }).success,
    ).toBe(false);
    expect(
      updateConfiguredMcpInputSchema.safeParse({
        ...update,
        credential_slots: [
          {
            ...update.credential_slots[0],
            payload_schema: {
              headers: ["Authorization"],
              query: ["key"],
            },
          },
        ],
      }).success,
    ).toBe(false);
  });

  test.each([
    "http://localhost:8771/api/mcp",
    "http://LOCALHOST:8771/api/mcp",
    "https://127.0.0.1:9443/mcp",
    "http://10.20.30.40/mcp",
    "http://[::1]:8771/api/mcp",
    "https://[fd00::1234]/mcp",
  ])(
    "accepts an HTTP(S) Project MCP endpoint on localhost or a canonical IP: %s",
    (url) => {
      const createInput = {
        slug: "internal-mcp",
        display_name: "Internal MCP",
        transport: "http" as const,
        url,
      };

      expect(
        createConfiguredMcpInputSchema.safeParse(createInput).success,
      ).toBe(true);
      expect(
        updateConfiguredMcpInputSchema.safeParse({
          transport: "http",
          url,
          expected_asset_version: 1,
        }).success,
      ).toBe(true);
    },
  );

  test.each([
    "http://user:password@localhost:8771/api/mcp",
    "http://localhost:8771/api/mcp?token=secret",
    "http://localhost:8771/api/mcp?",
    "http://localhost:8771/api/mcp#tools",
    "http://localhost:8771/api/mcp#",
    "ftp://trans-resource.internal/api/mcp",
    "http://localhost:0/api/mcp",
    "http://*/api/mcp",
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
  ])("rejects an unsafe or invalid Project MCP endpoint: %s", (url) => {
    expect(
      createConfiguredMcpInputSchema.safeParse({
        slug: "internal-mcp",
        display_name: "Internal MCP",
        transport: "http",
        url,
      }).success,
    ).toBe(false);
  });

  test("strictly binds the configured MCP aggregate to its final item and version", () => {
    const response = {
      item: {
        id: ASSET_ID,
        scope: "project",
        project_id: PROJECT_ID,
        slug: "amap-mcp",
        display_name: "高德地图 MCP",
        status: "active",
        current_published_version_id: VERSION_ID,
        version: 3,
        created_by_user_id: "user-1",
        created_at: "2026-08-02T00:00:00Z",
        updated_at: "2026-08-02T00:00:00Z",
      },
      version: {
        id: VERSION_ID,
        mcp_server_id: ASSET_ID,
        version_number: 1,
        workflow_status: "published",
        definition: {
          description: "地图与路线规划",
          transport: "http",
          command: null,
          args: [],
          url: "https://mcp.amap.com/mcp",
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
        payload_checksum: "b".repeat(64),
        submitted_at: null,
        reviewed_at: null,
        reviewed_by_user_id: null,
        created_by_user_id: "user-1",
        created_at: "2026-08-02T00:00:00Z",
      },
      request_id: "req-configured-mcp",
    };

    expect(configuredMcpResponseSchema.parse(response)).toEqual(response);
    expect(
      configuredMcpResponseSchema.safeParse({
        ...response,
        item: { ...response.item, id: "44444444-4444-4444-8444-444444444444" },
      }).success,
    ).toBe(false);
    expect(
      configuredMcpResponseSchema.safeParse({
        ...response,
        version: { ...response.version, workflow_status: "draft" },
      }).success,
    ).toBe(false);
  });

  test("strictly accepts a published editable MCP configuration with Credential slots", () => {
    const slotId = "44444444-4444-4444-8444-444444444444";
    const response = {
      item: {
        id: ASSET_ID,
        scope: "project",
        project_id: PROJECT_ID,
        slug: "transfer-mcp",
        display_name: "Transfer MCP",
        status: "active",
        current_published_version_id: VERSION_ID,
        version: 7,
        created_by_user_id: "user-1",
        created_at: "2026-08-02T00:00:00Z",
        updated_at: "2026-08-02T00:00:00Z",
      },
      version: {
        id: VERSION_ID,
        mcp_server_id: ASSET_ID,
        version_number: 3,
        workflow_status: "published",
        definition: {
          description: "Transfer resource MCP",
          transport: "http",
          command: null,
          args: [],
          url: "http://127.0.0.1:8771/api/mcp",
          env: {},
          headers: {},
          oauth: {},
          routing: {},
          tool_overrides: {},
          timeout_seconds: 30,
          credential_slots: [
            {
              name: "auth",
              purpose: "MCP request header authentication",
              payload_schema: { headers: ["Authorization"] },
              required: true,
            },
          ],
        },
        credential_slots: [
          {
            id: slotId,
            name: "auth",
            purpose: "MCP request header authentication",
            payload_schema: { headers: ["Authorization"] },
            required: true,
          },
        ],
        credential_grants: [],
        supersedes_version_id: null,
        payload_checksum: "b".repeat(64),
        submitted_at: "2026-08-02T00:00:00Z",
        reviewed_at: "2026-08-02T00:01:00Z",
        reviewed_by_user_id: "admin-1",
        created_by_user_id: "user-1",
        created_at: "2026-08-02T00:00:00Z",
      },
      request_id: "req-editable-mcp",
    };

    expect(
      projectMcpEditableConfigurationResponseSchema.parse(response),
    ).toEqual(response);
    expect(
      projectMcpEditableConfigurationResponseSchema.safeParse({
        ...response,
        item: {
          ...response.item,
          current_published_version_id: slotId,
        },
        version: {
          ...response.version,
          workflow_status: "pending_approval",
          supersedes_version_id: slotId,
        },
      }).success,
    ).toBe(true);
    expect(
      projectMcpEditableConfigurationResponseSchema.safeParse({
        ...response,
        version: { ...response.version, workflow_status: "draft" },
      }).success,
    ).toBe(false);
    expect(
      projectMcpEditableConfigurationResponseSchema.safeParse({
        ...response,
        private_locator: "must-not-enter-browser-cache",
      }).success,
    ).toBe(false);
  });

  test("strictly parses bounded MCP tool inventory metadata", () => {
    const response = {
      data: {
        status: "ready",
        tools: [
          {
            name: "maps_weather",
            description: "根据城市名称查询天气",
          },
        ],
        last_attempt_at: "2026-08-02T08:00:00Z",
        last_success_at: "2026-08-02T08:00:00Z",
        error_code: null,
      },
      request_id: "req-mcp-tools",
    };

    expect(mcpToolInventoryResponseSchema.parse(response)).toEqual(response);
    expect(
      mcpToolInventoryResponseSchema.safeParse({
        ...response,
        data: {
          ...response.data,
          tools: [{ name: "project secret", description: "invalid" }],
        },
      }).success,
    ).toBe(false);
    expect(
      mcpToolInventoryResponseSchema.safeParse({
        ...response,
        data: {
          ...response.data,
          tools: [response.data.tools[0], response.data.tools[0]],
        },
      }).success,
    ).toBe(false);
    expect(
      mcpToolInventoryResponseSchema.safeParse({
        ...response,
        data: {
          ...response.data,
          tools: Array.from({ length: 129 }, (_, index) => ({
            name: `tool_${index}`,
            description: "bounded",
          })),
        },
      }).success,
    ).toBe(false);
    expect(
      mcpToolInventoryResponseSchema.safeParse({
        ...response,
        data: { ...response.data, endpoint: "https://secret.example.test" },
      }).success,
    ).toBe(false);

    expect(
      mcpToolInventoryResponseSchema.parse({
        ...response,
        data: {
          ...response.data,
          status: "testing",
          error_code: null,
        },
      }).data,
    ).toEqual({
      ...response.data,
      status: "testing",
      error_code: null,
    });
    expect(
      mcpToolInventoryResponseSchema.safeParse({
        ...response,
        data: {
          ...response.data,
          status: "testing",
          error_code: "mcp_discovery_unavailable",
        },
      }).success,
    ).toBe(false);
  });

  test("strictly parses one bounded MCP tool-discovery attempt", () => {
    const response = {
      data: {
        id: "44444444-4444-4444-8444-444444444444",
        mcp_server_id: ASSET_ID,
        mcp_server_version_id: VERSION_ID,
        status: "queued",
        requested_at: "2026-08-03T08:00:00Z",
        started_at: null,
        completed_at: null,
        error_code: null,
      },
      request_id: "req-mcp-tool-discovery",
    };

    expect(mcpToolDiscoveryAttemptResponseSchema.parse(response)).toEqual(
      response,
    );
    expect(
      mcpToolDiscoveryAttemptResponseSchema.safeParse({
        ...response,
        data: { ...response.data, status: "pending" },
      }).success,
    ).toBe(false);
    expect(
      mcpToolDiscoveryAttemptResponseSchema.safeParse({
        ...response,
        data: {
          ...response.data,
          status: "failed",
          error_code: "private_upstream_error",
        },
      }).success,
    ).toBe(false);
  });

  test("strictly parses the project default Agent pointer and revision guard", () => {
    expect(
      projectDefaultAgentSchema.parse({
        agent_asset_id: ASSET_ID,
        revision: 2,
        request_id: "req-default-agent",
      }),
    ).toEqual({
      agent_asset_id: ASSET_ID,
      revision: 2,
      request_id: "req-default-agent",
    });
    expect(
      projectDefaultAgentSchema.parse({
        agent_asset_id: null,
        revision: 0,
        request_id: "req-main-fallback",
      }).agent_asset_id,
    ).toBeNull();
    expect(
      projectDefaultAgentSchema.safeParse({
        agent_asset_id: ASSET_ID,
        revision: 2,
        request_id: "req-default-agent",
        project_id: PROJECT_ID,
      }).success,
    ).toBe(false);
    expect(
      projectDefaultAgentInputSchema.parse({
        agent_asset_id: null,
        expected_revision: 0,
      }),
    ).toEqual({ agent_asset_id: null, expected_revision: 0 });
    expect(
      projectDefaultAgentInputSchema.safeParse({
        agent_asset_id: "not-a-uuid",
        expected_revision: 0,
      }).success,
    ).toBe(false);
    expect(
      projectDefaultAgentSchema.parse({
        agent_asset_id: ASSET_ID,
        revision: Number.MAX_SAFE_INTEGER,
        request_id: "req-max-safe-revision",
      }).revision,
    ).toBe(Number.MAX_SAFE_INTEGER);
    expect(
      projectDefaultAgentInputSchema.parse({
        agent_asset_id: ASSET_ID,
        expected_revision: Number.MAX_SAFE_INTEGER,
      }).expected_revision,
    ).toBe(Number.MAX_SAFE_INTEGER);
    expect(
      projectDefaultAgentSchema.safeParse({
        agent_asset_id: ASSET_ID,
        revision: Number.MAX_SAFE_INTEGER + 1,
        request_id: "req-unsafe-revision",
      }).success,
    ).toBe(false);
    expect(
      projectDefaultAgentInputSchema.safeParse({
        agent_asset_id: ASSET_ID,
        expected_revision: Number.MAX_SAFE_INTEGER + 1,
      }).success,
    ).toBe(false);
  });

  test("strictly parses bounded skill file previews and immutable fork changes", () => {
    const preview = skillVersionFileContentResponseSchema.parse({
      data: {
        path: "SKILL.md",
        media_type: "text/markdown",
        size_bytes: 24,
        sha256: "c".repeat(64),
        preview_status: "ready",
        encoding: "utf-8",
        content: "# Skill\n",
        source_payload_checksum: "d".repeat(64),
        asset_version: 3,
      },
      request_id: "req-skill-file",
    });

    expect(preview.data.content).toBe("# Skill\n");
    expect(
      skillVersionFileContentResponseSchema.safeParse({
        ...preview,
        data: { ...preview.data, preview_status: "binary", content: "raw" },
      }).success,
    ).toBe(false);

    const input = skillFileForkInputSchema.parse({
      expected_asset_version: 3,
      expected_source_payload_checksum: "d".repeat(64),
      changes: [
        {
          op: "replace",
          path: "SKILL.md",
          content: "# Updated\n",
          media_type: "text/markdown",
        },
        {
          op: "create",
          path: "references/guide.md",
          content: "Guide",
          media_type: "text/markdown",
        },
        { op: "delete", path: "references/old.md" },
      ],
    });
    expect(input.changes).toHaveLength(3);
    expect(
      skillFileForkInputSchema.safeParse({
        ...input,
        changes: [{ op: "delete", path: "SKILL.md", content: "forbidden" }],
      }).success,
    ).toBe(false);
  });

  test("strictly parses persisted project binding and item capabilities", () => {
    const base = {
      id: ASSET_ID,
      scope: "system",
      project_id: null,
      slug: "writer",
      display_name: "Writer",
      status: "active",
      current_published_version_id: VERSION_ID,
      version: 2,
      created_by_user_id: "user-1",
      created_at: "2026-07-14T00:00:00Z",
      updated_at: "2026-07-14T00:00:00Z",
      capabilities: ["shared_assets.read", "shared_assets.manage_bindings"],
      binding: {
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: ASSET_ID,
        version_id: VERSION_ID,
        enabled: true,
        version: 3,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
      },
    };

    const parsed = projectAssetListSchema.parse({
      system_items: [base],
      project_items: [
        {
          ...base,
          scope: "project",
          project_id: PROJECT_ID,
          binding: null,
        },
      ],
      request_id: "req-project-assets",
    });

    expect(parsed.system_items[0]?.binding?.version).toBe(3);
    expect(parsed.project_items[0]?.binding).toBeNull();
    expect(parsed.system_items[0]?.capabilities).toContain(
      "shared_assets.manage_bindings",
    );
  });

  test("keeps project Credential metadata secret-safe while accepting capabilities", () => {
    const parsed = projectCredentialListSchema.parse({
      system_items: [],
      project_items: [
        {
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
          capabilities: ["shared_assets.read"],
        },
      ],
      request_id: "req-project-credentials",
    });

    expect(parsed.project_items[0]?.capabilities).toEqual([
      "shared_assets.read",
    ]);
    expect(parsed.project_items[0]).not.toHaveProperty("plaintext");
  });

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
      agents_instructions: "# Agent\n\nReview changes carefully.",
      soul: "Be precise",
      identity: "# Identity\n\nYou are a reviewer.",
      user_context: "# User\n\nPrefer concise answers.",
      payload_schema_version: 3,
      model_ref: "default",
      model_settings: {
        temperature: 0.2,
        thinking_enabled: false,
      },
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
    expect(agentVersion).toMatchObject({
      agents_instructions: "# Agent\n\nReview changes carefully.",
      model_settings: {
        temperature: 0.2,
        thinking_enabled: false,
      },
      soul: "Be precise",
      identity: "# Identity\n\nYou are a reviewer.",
      user_context: "# User\n\nPrefer concise answers.",
      payload_schema_version: 3,
    });

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

  test("keeps Skill and MCP authoring payloads distinct and credential secrets write-only", () => {
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
    const input = createCredentialInputSchema.parse({
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    });
    expect(input.payload.env?.TOKEN).toBe("write-only");
    expect(input).not.toHaveProperty("ciphertext");
  });

  test("strictly validates the four virtual Agent instruction documents and revision", () => {
    const input = {
      agents_instructions: "# AGENTS.md",
      soul: "# SOUL.md",
      identity: "# IDENTITY.md",
      user_context: "# USER.md",
      expected_asset_version: 7,
    };

    expect(agentInstructionsInputSchema.parse(input)).toEqual(input);
    expect(
      agentInstructionsInputSchema.safeParse({
        ...input,
        expected_asset_version: 0,
      }).success,
    ).toBe(false);
    expect(
      agentInstructionsInputSchema.safeParse({
        ...input,
        path: "AGENTS.md",
      }).success,
    ).toBe(false);
  });

  test("accepts only non-empty multi-field Credential payload sections", () => {
    expect(
      credentialPayloadSchema.parse({
        env: {
          GITHUB_TOKEN: "write-only-token",
          GITHUB_ORG: "deer-flow",
        },
        headers: { Authorization: "write-only-header" },
        query: { key: "write-only-query-key" },
        oauth: { refresh_token: "write-only-refresh-token" },
      }),
    ).toEqual({
      env: {
        GITHUB_TOKEN: "write-only-token",
        GITHUB_ORG: "deer-flow",
      },
      headers: { Authorization: "write-only-header" },
      query: { key: "write-only-query-key" },
      oauth: { refresh_token: "write-only-refresh-token" },
    });

    for (const payload of [
      {},
      { env: {} },
      { env: { "": "write-only" } },
      { env: { ["x".repeat(256)]: "write-only" } },
      { cookies: { session: "write-only" } },
      { oauth: { token: { nested: "not-supported" } } },
    ]) {
      expect(() => credentialPayloadSchema.parse(payload)).toThrow();
    }
  });

  test("strictly validates Credential delete revisions", () => {
    expect(
      deleteCredentialInputSchema.parse({
        expected_credential_version: 7,
      }),
    ).toEqual({ expected_credential_version: 7 });
    expect(
      deleteCredentialInputSchema.safeParse({
        expected_credential_version: 0,
      }).success,
    ).toBe(false);
    expect(
      deleteCredentialInputSchema.safeParse({
        expected_credential_version: 7,
        secret: "must-never-be-sent",
      }).success,
    ).toBe(false);
  });

  test("restricts Credential version field schemas to supported payload sections", () => {
    const version = {
      id: VERSION_ID,
      credential_id: ASSET_ID,
      version_number: 1,
      status: "active",
      payload_schema_version: 1,
      payload_schema: {
        env: ["GITHUB_TOKEN", "GITHUB_ORG"],
        headers: ["Authorization"],
        oauth: ["refresh_token"],
      },
      supersedes_version_id: null,
      created_by_user_id: "user-1",
      created_at: "2026-07-14T00:00:00Z",
    };

    expect(credentialVersionSchema.parse(version).payload_schema).toEqual(
      version.payload_schema,
    );
    expect(() =>
      credentialVersionSchema.parse({
        ...version,
        payload_schema: { cookies: ["session"] },
      }),
    ).toThrow();
    expect(() =>
      credentialVersionSchema.parse({
        ...version,
        payload_schema: { env: [] },
      }),
    ).toThrow();
    expect(() =>
      credentialVersionSchema.parse({
        ...version,
        payload_schema: { env: [""] },
      }),
    ).toThrow();
  });

  test("rejects invalid Skill and MCP wire enums and definition slot schemas", () => {
    const skillVersion = {
      id: VERSION_ID,
      skill_id: ASSET_ID,
      version_number: 1,
      workflow_status: "published",
      description: "Skill",
      frontmatter: {},
      compatibility: null,
      secret_requirements: [],
      scan_decision: "allow",
      scan_rule_ids: [],
      scan_summary: {},
      file_views: [],
      supersedes_version_id: null,
      payload_checksum: "a".repeat(64),
      created_by_user_id: "user-1",
      created_at: "2026-07-14T00:00:00Z",
    };
    expect(skillVersionSchema.parse(skillVersion).scan_decision).toBe("allow");
    expect(() =>
      skillVersionSchema.parse({ ...skillVersion, scan_decision: "permit" }),
    ).toThrow();

    const mcpVersion = {
      id: VERSION_ID,
      mcp_server_id: ASSET_ID,
      version_number: 1,
      workflow_status: "published",
      definition: {
        description: "MCP",
        transport: "streamable_http",
        command: null,
        args: [],
        url: "https://mcp.example.test",
        env: {},
        headers: {},
        oauth: {},
        routing: {},
        tool_overrides: {},
        timeout_seconds: 30,
        credential_slots: [
          {
            name: "primary",
            purpose: "Authentication",
            payload_schema: { env: ["TOKEN"] },
            required: true,
          },
        ],
      },
      credential_slots: [],
      credential_grants: [
        {
          id: "44444444-4444-4444-8444-444444444444",
          mcp_server_version_id: VERSION_ID,
          credential_slot_id: "55555555-5555-4555-8555-555555555555",
          credential_version_id: "66666666-6666-4666-8666-666666666666",
          status: "active",
          version: 1,
          created_by_user_id: "user-1",
          created_at: "2026-07-14T00:00:00Z",
        },
      ],
      supersedes_version_id: null,
      payload_checksum: "b".repeat(64),
      submitted_at: null,
      reviewed_at: null,
      reviewed_by_user_id: null,
      created_by_user_id: "user-1",
      created_at: "2026-07-14T00:00:00Z",
    };
    expect(mcpVersionSchema.parse(mcpVersion).definition.transport).toBe(
      "streamable_http",
    );
    expect(() =>
      mcpVersionSchema.parse({
        ...mcpVersion,
        definition: { ...mcpVersion.definition, transport: "websocket" },
      }),
    ).toThrow();
    expect(() =>
      mcpVersionSchema.parse({
        ...mcpVersion,
        definition: {
          ...mcpVersion.definition,
          credential_slots: [
            {
              ...mcpVersion.definition.credential_slots[0],
              payload_schema: { env: "TOKEN" },
            },
          ],
        },
      }),
    ).toThrow();
    expect(() =>
      mcpVersionSchema.parse({
        ...mcpVersion,
        credential_grants: [
          { ...mcpVersion.credential_grants[0], status: "pending" },
        ],
      }),
    ).toThrow();
  });

  test.each(["stdio", "sse", "http", "streamable_http"])(
    "accepts supported MCP authoring transport %s",
    (transport) => {
      expect(
        mcpVersionInputSchema.parse({
          transport,
          credential_slots: [
            {
              name: "primary",
              payload_schema: { env: ["TOKEN"] },
            },
          ],
          expected_asset_version: 1,
        }).transport,
      ).toBe(transport);
    },
  );

  test("rejects unsupported MCP authoring transport and malformed slot schema", () => {
    expect(() =>
      mcpVersionInputSchema.parse({
        transport: "websocket",
        expected_asset_version: 1,
      }),
    ).toThrow();
    expect(() =>
      mcpVersionInputSchema.parse({
        transport: "http",
        credential_slots: [
          { name: "primary", payload_schema: { env: "TOKEN" } },
        ],
        expected_asset_version: 1,
      }),
    ).toThrow();
    for (const name of [
      "Primary",
      "1primary",
      "primary slot",
      `a${"b".repeat(63)}`,
    ]) {
      expect(() =>
        mcpVersionInputSchema.parse({
          transport: "http",
          credential_slots: [{ name, payload_schema: { env: ["TOKEN"] } }],
          expected_asset_version: 1,
        }),
      ).toThrow();
    }
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
