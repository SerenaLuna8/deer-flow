import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  SharedAssetApiError,
  approveAdminProjectMcpVersion,
  approveProjectMcpVersion,
  changeProjectAssetStatus,
  configureAdminMcpCredentialGrants,
  createAdminCredential,
  createAdminProjectAsset,
  createAdminProjectCredential,
  createConfiguredProjectMcp,
  createProjectAsset,
  createProjectAssetVersion,
  createProjectCredential,
  deleteAdminCredential,
  deleteAdminProjectCredential,
  deleteProjectCredential,
  deleteProjectAgent,
  deleteProjectMcp,
  deleteProjectSkill,
  disableProjectSystemBinding,
  enableProjectSystemBinding,
  enableAdminProjectSystemBinding,
  forkProjectSkillVersion,
  getProjectDefaultAgent,
  getProjectMcpEditableConfiguration,
  getProjectMcpToolInventory,
  getProjectSkillVersionFile,
  importProjectSkillArchive,
  listAdminAssets,
  listAdminProjectAssets,
  listProjectAssetVersions,
  listProjectAssets,
  listSystemAssetCatalog,
  migrateProjectCredentialGrants,
  publishProjectAssetVersion,
  requestProjectMcpToolDiscovery,
  replaceProjectCredential,
  revokeProjectCredential,
  setProjectDefaultAgent,
  syncCurrentProjectSystemMcpBinding,
  submitProjectMcpVersion,
  updateConfiguredProjectMcp,
  updateProjectAgentInstructions,
  upgradeProjectSystemBinding,
} from "@/core/shared-assets/api";

const mockedFetch = rs.mocked(fetchWithAuth);
const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const asset = {
  id: "11111111-1111-4111-8111-111111111111",
  scope: "project",
  project_id: PROJECT_ID,
  slug: "writer",
  display_name: "Writer",
  status: "active",
  current_published_version_id: null,
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z",
};
const projectAsset = {
  ...asset,
  capabilities: ["shared_assets.read"],
  binding: null,
};
const versionId = "22222222-2222-4222-8222-222222222222";
const agentVersion = {
  id: versionId,
  agent_id: asset.id,
  version_number: 1,
  workflow_status: "published",
  description: "Review changes",
  agents_instructions: "# Agent\n\nReview changes carefully.",
  soul: "Be precise",
  identity: "# Identity\n\nYou are a reviewer.",
  user_context: "# User\n\nPrefer concise answers.",
  payload_schema_version: 2,
  model_ref: "default",
  tool_groups: ["web"],
  skill_version_ids: [],
  mcp_version_ids: [],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
};
const credentialVersion = {
  id: versionId,
  credential_id: asset.id,
  version_number: 1,
  status: "active",
  payload_schema_version: 1,
  payload_schema: { env: ["TOKEN"] },
  supersedes_version_id: null,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
};
const mcpVersion = {
  id: versionId,
  mcp_server_id: asset.id,
  version_number: 1,
  workflow_status: "published",
  definition: {
    description: "GitHub MCP",
    transport: "stdio",
    command: "github-mcp",
    args: [],
    url: null,
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
  submitted_at: "2026-07-14T00:00:00Z",
  reviewed_at: "2026-07-14T00:01:00Z",
  reviewed_by_user_id: "reviewer-1",
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
};
const credential = {
  id: asset.id,
  scope: "project",
  project_id: PROJECT_ID,
  name: "github",
  display_name: "GitHub",
  credential_type: "token",
  status: "revoked",
  current_version_id: versionId,
  version: 2,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:01:00Z",
};
const binding = {
  project_id: PROJECT_ID,
  kind: "mcp",
  asset_id: asset.id,
  version_id: versionId,
  enabled: true,
  version: 1,
  created_by_user_id: "user-1",
  updated_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z",
  request_id: "req-binding",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("shared asset api", () => {
  test("loads the exact current editable Project MCP configuration", async () => {
    const slotId = "44444444-4444-4444-8444-444444444444";
    const response = {
      item: {
        ...asset,
        current_published_version_id: versionId,
        version: 7,
      },
      version: {
        ...mcpVersion,
        definition: {
          ...mcpVersion.definition,
          transport: "http",
          command: null,
          url: "http://127.0.0.1:8771/api/mcp",
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
      },
      request_id: "req-editable-mcp",
    };
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, response));
    const signal = new AbortController().signal;

    await expect(
      getProjectMcpEditableConfiguration(PROJECT_ID, asset.id, signal),
    ).resolves.toEqual(response);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/configured`,
      { signal },
    );

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        ...response,
        item: { ...response.item, project_id: versionId },
      }),
    );
    await expect(
      getProjectMcpEditableConfiguration(PROJECT_ID, asset.id, signal),
    ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });
  });

  test("adds one configured project MCP with one flat request", async () => {
    const input = {
      slug: "amap-mcp",
      display_name: "高德地图 MCP",
      description: "地图与路线规划",
      transport: "http" as const,
      command: null,
      args: [],
      url: "https://10.0.0.8/mcp",
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
    const response = {
      item: {
        ...asset,
        slug: input.slug,
        display_name: input.display_name,
        version: 3,
      },
      version: {
        ...mcpVersion,
        workflow_status: "pending_approval",
        definition: {
          ...mcpVersion.definition,
          description: input.description,
          transport: input.transport,
          command: null,
          url: input.url,
          credential_slots: input.credential_slots,
        },
        credential_slots: input.credential_slots.map((slot) => ({
          id: "44444444-4444-4444-8444-444444444444",
          ...slot,
        })),
        submitted_at: "2026-07-14T00:00:00Z",
        reviewed_at: null,
        reviewed_by_user_id: null,
      },
      request_id: "req-configured-mcp",
    };
    mockedFetch.mockResolvedValueOnce(jsonResponse(201, response));
    const signal = new AbortController().signal;

    await expect(
      createConfiguredProjectMcp(PROJECT_ID, input, signal),
    ).resolves.toEqual(response);
    expect(mockedFetch).toHaveBeenCalledTimes(1);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/configured`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: expect.any(String),
        signal,
      },
    );
    const requestBody = mockedFetch.mock.calls[0]?.[1]?.body;
    expect(typeof requestBody).toBe("string");
    if (typeof requestBody !== "string") {
      throw new TypeError("Expected a JSON request body");
    }
    expect(JSON.parse(requestBody)).toEqual(input);

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, {
        ...response,
        item: { ...response.item, version: 91 },
      }),
    );
    await expect(
      createConfiguredProjectMcp(PROJECT_ID, input, signal),
    ).resolves.toMatchObject({ item: { version: 91 } });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, {
        ...response,
        item: {
          ...response.item,
          current_published_version_id: response.version.id,
        },
        version: { ...response.version, workflow_status: "published" },
      }),
    );
    await expect(
      createConfiguredProjectMcp(PROJECT_ID, input, signal),
    ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, {
        ...response,
        version: {
          ...response.version,
          supersedes_version_id: "99999999-9999-4999-8999-999999999999",
        },
      }),
    );
    await expect(
      createConfiguredProjectMcp(PROJECT_ID, input, signal),
    ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });
  });

  test("updates one configured project MCP without creating a selectable revision", async () => {
    const input = {
      description: "更新后的地图服务",
      transport: "http" as const,
      command: null,
      args: [],
      url: "https://10.0.0.8/mcp",
      env: {},
      headers: {},
      oauth: {},
      routing: {},
      tool_overrides: {},
      timeout_seconds: 30,
      credential_slots: [],
      expected_asset_version: 3,
    };
    const response = {
      item: {
        ...asset,
        current_published_version_id: mcpVersion.id,
        version: 5,
      },
      version: {
        ...mcpVersion,
        definition: {
          ...mcpVersion.definition,
          description: input.description,
          transport: input.transport,
          command: null,
          url: input.url,
        },
        submitted_at: null,
        reviewed_at: null,
        reviewed_by_user_id: null,
      },
      request_id: "req-update-configured-mcp",
    };
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, response));
    const signal = new AbortController().signal;

    await expect(
      updateConfiguredProjectMcp(PROJECT_ID, asset.id, input, signal),
    ).resolves.toEqual(response);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/configured`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: expect.any(String),
        signal,
      },
    );
    const updateRequestBody = mockedFetch.mock.calls[0]?.[1]?.body;
    expect(typeof updateRequestBody).toBe("string");
    if (typeof updateRequestBody !== "string") {
      throw new TypeError("Expected a JSON request body");
    }
    expect(JSON.parse(updateRequestBody)).toEqual(input);

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        ...response,
        item: { ...response.item, version: 4 },
      }),
    );
    await expect(
      updateConfiguredProjectMcp(PROJECT_ID, asset.id, input, signal),
    ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        ...response,
        item: { ...response.item, current_published_version_id: null },
        version: {
          ...response.version,
          workflow_status: "pending_approval",
          submitted_at: "2026-07-14T00:00:00Z",
        },
      }),
    );
    await expect(
      updateConfiguredProjectMcp(PROJECT_ID, asset.id, input, signal),
    ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });

    const credentialInput = {
      ...input,
      credential_slots: [
        {
          name: "api-key",
          purpose: "API Key",
          payload_schema: { query: ["key"] },
          required: true,
        },
      ],
    };
    const oldPublishedVersionId = "77777777-7777-4777-8777-777777777777";
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        item: {
          ...asset,
          current_published_version_id: oldPublishedVersionId,
          version: 5,
        },
        version: {
          ...mcpVersion,
          workflow_status: "pending_approval",
          definition: {
            ...mcpVersion.definition,
            description: credentialInput.description,
            transport: credentialInput.transport,
            url: credentialInput.url,
            credential_slots: credentialInput.credential_slots,
          },
          credential_slots: credentialInput.credential_slots.map((slot) => ({
            ...slot,
            id: "88888888-8888-4888-8888-888888888888",
          })),
          supersedes_version_id: "99999999-9999-4999-8999-999999999999",
          submitted_at: "2026-07-14T00:00:00Z",
          reviewed_at: null,
          reviewed_by_user_id: null,
        },
        request_id: "req-stale-configured-mcp",
      }),
    );
    await expect(
      updateConfiguredProjectMcp(PROJECT_ID, asset.id, credentialInput, signal),
    ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });
  });

  test("syncs a System MCP binding to the server-authoritative current configuration", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, binding));
    const input = { expected_binding_version: 2 };
    const signal = new AbortController().signal;

    await expect(
      syncCurrentProjectSystemMcpBinding(PROJECT_ID, asset.id, input, signal),
    ).resolves.toEqual(binding);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/system-mcp-bindings/${asset.id}/sync-current`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
        signal,
      },
    );

    for (const invalidBinding of [
      { ...binding, kind: "agent" },
      {
        ...binding,
        project_id: "77777777-7777-4777-8777-777777777777",
      },
      {
        ...binding,
        asset_id: "88888888-8888-4888-8888-888888888888",
      },
    ]) {
      mockedFetch.mockResolvedValueOnce(jsonResponse(200, invalidBinding));
      await expect(
        syncCurrentProjectSystemMcpBinding(PROJECT_ID, asset.id, input, signal),
      ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });
    }
  });

  test("loads only the project-scoped display inventory for one exact MCP version", async () => {
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
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, response));
    const signal = new AbortController().signal;

    await expect(
      getProjectMcpToolInventory(PROJECT_ID, asset.id, versionId, signal),
    ).resolves.toEqual(response);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/tools`,
      { signal },
    );
  });

  test("queues tool discovery for one exact project MCP version", async () => {
    const response = {
      data: {
        id: "44444444-4444-4444-8444-444444444444",
        mcp_server_id: asset.id,
        mcp_server_version_id: versionId,
        status: "queued",
        requested_at: "2026-08-03T08:00:00Z",
        started_at: null,
        completed_at: null,
        error_code: null,
      },
      request_id: "req-mcp-tool-discovery",
    };
    mockedFetch.mockResolvedValueOnce(jsonResponse(202, response));
    const signal = new AbortController().signal;

    await expect(
      requestProjectMcpToolDiscovery(PROJECT_ID, asset.id, versionId, signal),
    ).resolves.toEqual(response);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/tool-discovery`,
      { method: "POST", signal },
    );

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(202, {
        ...response,
        data: { ...response.data, mcp_server_version_id: asset.id },
      }),
    );
    await expect(
      requestProjectMcpToolDiscovery(PROJECT_ID, asset.id, versionId, signal),
    ).rejects.toMatchObject({ code: "ASSET_RESPONSE_INVALID" });
  });

  test("uses only project-scoped admin override routes for shared assets credentials approvals and bindings", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          system_items: [],
          project_items: [projectAsset],
          request_id: "req-override-list",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(201, { item: asset, request_id: "req-override-create" }),
      )
      .mockResolvedValueOnce(
        jsonResponse(201, {
          item: { ...credential, status: "active" },
          request_id: "req-override-credential",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          data: mcpVersion,
          request_id: "req-override-approval",
        }),
      )
      .mockResolvedValueOnce(jsonResponse(201, binding));

    await listAdminProjectAssets(PROJECT_ID, "agents");
    await createAdminProjectAsset(PROJECT_ID, "agents", {
      slug: "reviewer",
      display_name: "Reviewer",
    });
    const createdCredential = await createAdminProjectCredential(PROJECT_ID, {
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    });
    await approveAdminProjectMcpVersion(PROJECT_ID, asset.id, versionId, {
      credential_versions: { primary: versionId },
      expected_asset_version: 2,
    });
    await enableAdminProjectSystemBinding(PROJECT_ID, "agent", {
      asset_id: asset.id,
      version_id: versionId,
    });

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/admin/projects/${PROJECT_ID}/assets/agents`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/agents`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/credentials`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/mcp-servers/${asset.id}/versions/${versionId}/approve`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/system-agent-bindings`,
    ]);
    expect(
      mockedFetch.mock.calls.some(([url]) =>
        (typeof url === "string"
          ? url
          : url instanceof URL
            ? url.href
            : url.url
        ).includes(`/api/projects/${PROJECT_ID}/`),
      ),
    ).toBe(false);
    expect(createdCredential.item).not.toHaveProperty("payload");
    expect(createdCredential.item).not.toHaveProperty("plaintext");
  });

  test("configures only Credential grants on a published packaged System MCP version", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        data: mcpVersion,
        request_id: "req-system-mcp-grants",
      }),
    );
    const input = {
      credential_versions: { primary: versionId },
      expected_active_grant_versions: { primary: 2 },
    };

    await configureAdminMcpCredentialGrants(asset.id, versionId, input);

    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/admin/assets/mcp-servers/${asset.id}/versions/${versionId}/credential-grants`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
  });

  test("loads one skill file lazily and forks edits without sending unchanged files", async () => {
    const sourceChecksum = "d".repeat(64);
    const fileResponse = {
      data: {
        path: "references/guide.md",
        media_type: "text/markdown",
        size_bytes: 5,
        sha256: "c".repeat(64),
        preview_status: "ready",
        encoding: "utf-8",
        content: "Guide",
        source_payload_checksum: sourceChecksum,
        asset_version: 3,
      },
      request_id: "req-file",
    };
    const skillVersion = {
      id: versionId,
      skill_id: asset.id,
      version_number: 2,
      workflow_status: "draft",
      description: "Updated",
      frontmatter: {},
      compatibility: null,
      secret_requirements: [],
      scan_decision: "allow",
      scan_rule_ids: [],
      scan_summary: {},
      file_views: [],
      supersedes_version_id: versionId,
      payload_checksum: "e".repeat(64),
      created_by_user_id: "user-1",
      created_at: "2026-07-22T00:00:00Z",
    };
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(200, fileResponse))
      .mockResolvedValueOnce(
        jsonResponse(201, {
          data: skillVersion,
          request_id: "req-fork",
        }),
      );
    const signal = new AbortController().signal;

    await expect(
      getProjectSkillVersionFile(
        PROJECT_ID,
        asset.id,
        versionId,
        "references/guide.md",
        signal,
      ),
    ).resolves.toEqual(fileResponse);
    await forkProjectSkillVersion(PROJECT_ID, asset.id, versionId, {
      expected_asset_version: 3,
      expected_source_payload_checksum: sourceChecksum,
      changes: [
        {
          op: "replace",
          path: "references/guide.md",
          content: "Updated guide",
          media_type: "text/markdown",
        },
      ],
    });

    expect(mockedFetch.mock.calls[0]).toEqual([
      `/backend/api/projects/${PROJECT_ID}/skills/${asset.id}/versions/${versionId}/files/content?path=references%2Fguide.md`,
      { signal },
    ]);
    expect(mockedFetch.mock.calls[1]).toEqual([
      `/backend/api/projects/${PROJECT_ID}/skills/${asset.id}/versions/${versionId}/fork`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expected_asset_version: 3,
          expected_source_payload_checksum: sourceChecksum,
          changes: [
            {
              op: "replace",
              path: "references/guide.md",
              content: "Updated guide",
              media_type: "text/markdown",
            },
          ],
        }),
      }),
    ]);
  });

  test("imports one project Skill archive as multipart without overriding its content type", async () => {
    const importedVersion = {
      id: versionId,
      skill_id: asset.id,
      version_number: 1,
      workflow_status: "published",
      description: "Meeting notes",
      frontmatter: { name: "meeting-brief" },
      compatibility: null,
      secret_requirements: [],
      scan_decision: "allow",
      scan_rule_ids: [],
      scan_summary: {},
      file_views: [
        {
          path: "SKILL.md",
          media_type: "text/markdown",
          size_bytes: 24,
          sha256: "c".repeat(64),
        },
      ],
      supersedes_version_id: null,
      payload_checksum: "d".repeat(64),
      created_by_user_id: "user-1",
      created_at: "2026-07-26T00:00:00Z",
    };
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, {
        item: {
          ...asset,
          slug: "meeting-brief",
          display_name: "meeting-brief",
          status: "suspended",
        },
        version: importedVersion,
        request_id: "req-skill-import",
      }),
    );
    const archive = new File(["archive"], "meeting-brief.zip", {
      type: "application/zip",
    });
    const signal = new AbortController().signal;

    const result = await importProjectSkillArchive(PROJECT_ID, archive, signal);

    expect(result.version.id).toBe(versionId);
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/skills/import`,
      expect.objectContaining({
        method: "POST",
        signal,
      }),
    );
    const init = mockedFetch.mock.calls[0]?.[1];
    expect(init?.body).toBeInstanceOf(FormData);
    expect((init?.body as FormData).get("archive")).toMatchObject({
      name: archive.name,
      size: archive.size,
      type: archive.type,
    });
    expect(init?.headers).toBeUndefined();
  });

  test("maps both known envelopes and plain HTTP 413 upload limits safely", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(413, {
          detail: {
            code: "skill_archive_upload_too_large",
            message: "private upload limit",
          },
        }),
      )
      .mockResolvedValueOnce(
        new Response("payload too large", {
          status: 413,
          headers: { "Content-Type": "text/plain" },
        }),
      );
    const archive = new File(["archive"], "meeting-brief.zip");

    for (let index = 0; index < 2; index += 1) {
      await expect(
        importProjectSkillArchive(PROJECT_ID, archive),
      ).rejects.toMatchObject({
        status: 413,
        code: "ASSET_UPLOAD_TOO_LARGE",
        message: "Skill archive upload too large",
      });
    }
  });

  test("uses only the authenticated fetcher for project, admin, and system catalog lists", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          system_items: [],
          project_items: [projectAsset],
          request_id: "req-1",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [{ ...asset, scope: "system", project_id: null }],
          request_id: "req-2",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [{ ...asset, scope: "system", project_id: null }],
          request_id: "req-3",
        }),
      );
    const signal = new AbortController().signal;

    await expect(
      listProjectAssets(PROJECT_ID, "agents", signal),
    ).resolves.toMatchObject({ project_items: [projectAsset] });
    await expect(listAdminAssets("agents", signal)).resolves.toMatchObject({
      items: [{ id: asset.id }],
    });
    await expect(
      listSystemAssetCatalog("agents", signal),
    ).resolves.toMatchObject({ items: [{ id: asset.id }] });
    expect(mockedFetch.mock.calls).toEqual([
      [`/backend/api/projects/${PROJECT_ID}/agents`, { signal }],
      ["/backend/api/admin/assets/agents", { signal }],
      ["/backend/api/assets/catalog/agents", { signal }],
    ]);
  });

  test("reads and revision-guards the project default Agent setting", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          agent_asset_id: asset.id,
          revision: 4,
          request_id: "req-default-agent-read",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          agent_asset_id: null,
          revision: 5,
          request_id: "req-default-agent-write",
        }),
      );
    const signal = new AbortController().signal;

    await expect(getProjectDefaultAgent(PROJECT_ID, signal)).resolves.toEqual({
      agent_asset_id: asset.id,
      revision: 4,
      request_id: "req-default-agent-read",
    });
    await expect(
      setProjectDefaultAgent(
        PROJECT_ID,
        { agent_asset_id: null, expected_revision: 4 },
        signal,
      ),
    ).resolves.toMatchObject({ agent_asset_id: null, revision: 5 });
    expect(mockedFetch.mock.calls).toEqual([
      [`/backend/api/projects/${PROJECT_ID}/default-agent`, { signal }],
      [
        `/backend/api/projects/${PROJECT_ID}/default-agent`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            agent_asset_id: null,
            expected_revision: 4,
          }),
          signal,
        },
      ],
    ]);
  });

  test("rejects an unsafe default-Agent revision before sending a request", async () => {
    await expect(
      setProjectDefaultAgent(PROJECT_ID, {
        agent_asset_id: asset.id,
        expected_revision: Number.MAX_SAFE_INTEGER + 1,
      }),
    ).rejects.toThrow();
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  test("validates mutation input before sending it through the authenticated fetcher", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, { item: asset, request_id: "req-3" }),
    );
    await createProjectAsset(PROJECT_ID, "agents", {
      slug: "writer",
      display_name: "Writer",
    });
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/agents`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ slug: "writer", display_name: "Writer" }),
      }),
    );
    await expect(
      createProjectAsset("not-a-uuid", "agents", {
        slug: "writer",
        display_name: "Writer",
      }),
    ).rejects.toMatchObject({ code: "ASSET_VALIDATION_FAILED" });
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  test("updates all four virtual Agent instruction documents with one revision-guarded PUT", async () => {
    const input = {
      agents_instructions: "# AGENTS.md",
      soul: "# SOUL.md",
      identity: "# IDENTITY.md",
      user_context: "# USER.md",
      expected_asset_version: 7,
    };
    const {
      expected_asset_version: _expectedAssetVersion,
      ...instructionDocuments
    } = input;
    void _expectedAssetVersion;
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        data: {
          ...agentVersion,
          ...instructionDocuments,
          version_number: 2,
        },
        request_id: "req-agent-instructions",
      }),
    );
    const signal = new AbortController().signal;

    await expect(
      updateProjectAgentInstructions(PROJECT_ID, asset.id, input, signal),
    ).resolves.toMatchObject({
      data: {
        agent_id: asset.id,
        agents_instructions: input.agents_instructions,
        soul: input.soul,
        identity: input.identity,
        user_context: input.user_context,
      },
    });
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/agents/${asset.id}/instructions`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
        signal,
      },
    );
  });

  test("permanently deletes one project Skill package with its expected revision", async () => {
    mockedFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
    const signal = new AbortController().signal;
    const input = { expected_asset_version: 7 };

    await expect(
      deleteProjectSkill(PROJECT_ID, asset.id, input, signal),
    ).resolves.toBeUndefined();

    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/skills/${asset.id}`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
        signal,
      },
    );
    expect(() =>
      changeProjectAssetStatus(
        PROJECT_ID,
        "skills",
        asset.id,
        "archive" as never,
        input,
      ),
    ).toThrow(
      expect.objectContaining({
        code: "ASSET_VALIDATION_FAILED",
      }),
    );
    expect(() =>
      changeProjectAssetStatus(
        PROJECT_ID,
        "agents",
        asset.id,
        "archive" as never,
        input,
      ),
    ).toThrow(
      expect.objectContaining({
        code: "ASSET_VALIDATION_FAILED",
      }),
    );
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  test("permanently deletes one project Agent package with its expected revision", async () => {
    mockedFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
    const signal = new AbortController().signal;
    const input = { expected_asset_version: 9 };

    await expect(
      deleteProjectAgent(PROJECT_ID, asset.id, input, signal),
    ).resolves.toBeUndefined();

    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/agents/${asset.id}`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
        signal,
      },
    );
  });

  test("permanently deletes one project MCP with its expected revision", async () => {
    mockedFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
    const signal = new AbortController().signal;
    const input = { expected_asset_version: 11 };

    await expect(
      deleteProjectMcp(PROJECT_ID, asset.id, input, signal),
    ).resolves.toBeUndefined();

    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
        signal,
      },
    );
  });

  test("logically deletes Credential metadata through each scoped route", async () => {
    mockedFetch
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const signal = new AbortController().signal;
    const input = { expected_credential_version: 9 };

    await expect(
      deleteProjectCredential(PROJECT_ID, asset.id, input, signal),
    ).resolves.toBeUndefined();
    await expect(
      deleteAdminProjectCredential(PROJECT_ID, asset.id, input, signal),
    ).resolves.toBeUndefined();
    await expect(
      deleteAdminCredential(asset.id, input, signal),
    ).resolves.toBeUndefined();

    expect(mockedFetch.mock.calls).toEqual([
      [
        `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}`,
        {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
          signal,
        },
      ],
      [
        `/backend/api/admin/projects/${PROJECT_ID}/assets/credentials/${asset.id}`,
        {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
          signal,
        },
      ],
      [
        `/backend/api/admin/assets/credentials/${asset.id}`,
        {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
          signal,
        },
      ],
    ]);
  });

  test("activates and suspends a project Skill through explicit status actions", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          item: {
            ...asset,
            status: "active",
            current_published_version_id: versionId,
          },
          request_id: "req-skill-activate",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          item: {
            ...asset,
            status: "suspended",
            current_published_version_id: versionId,
            version: 2,
          },
          request_id: "req-skill-suspend",
        }),
      );
    const input = { expected_asset_version: 1 };

    await changeProjectAssetStatus(
      PROJECT_ID,
      "skills",
      asset.id,
      "activate",
      input,
    );
    await changeProjectAssetStatus(
      PROJECT_ID,
      "skills",
      asset.id,
      "suspend",
      input,
    );

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      `/backend/api/projects/${PROJECT_ID}/skills/${asset.id}/activate`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      `/backend/api/projects/${PROJECT_ID}/skills/${asset.id}/suspend`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
  });

  test("activates and suspends project MCP without exposing archive", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          item: {
            ...asset,
            status: "active",
            current_published_version_id: versionId,
          },
          request_id: "req-mcp-activate",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          item: {
            ...asset,
            status: "suspended",
            current_published_version_id: versionId,
            version: 2,
          },
          request_id: "req-mcp-suspend",
        }),
      );
    const input = { expected_asset_version: 1 };

    await changeProjectAssetStatus(
      PROJECT_ID,
      "mcp-servers",
      asset.id,
      "activate",
      input,
    );
    await changeProjectAssetStatus(
      PROJECT_ID,
      "mcp-servers",
      asset.id,
      "suspend",
      input,
    );

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/activate`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/suspend`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
    expect(() =>
      changeProjectAssetStatus(
        PROJECT_ID,
        "mcp-servers",
        asset.id,
        "archive" as never,
        input,
      ),
    ).toThrow(expect.objectContaining({ code: "ASSET_VALIDATION_FAILED" }));
  });

  test("uses canonical public errors and rejects unsafe responses", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(403, {
        detail: {
          code: "asset_forbidden",
          message: "SQL secret should not escape",
          request_id: "req-4",
        },
      }),
    );
    const error = await listAdminAssets("skills").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(SharedAssetApiError);
    expect(error).toMatchObject({
      status: 403,
      code: "ASSET_FORBIDDEN",
      message: "Asset capability required",
    });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(429, {
        detail: {
          code: "asset_storage_quota_exceeded",
          message: "private quota calculation must not escape",
          request_id: "req-quota",
        },
      }),
    );
    await expect(listAdminAssets("skills")).rejects.toMatchObject({
      status: 429,
      code: "ASSET_STORAGE_QUOTA_EXCEEDED",
      message: "Project Skill storage quota exceeded",
    });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        items: [
          {
            ...asset,
            scope: "system",
            project_id: null,
            plaintext: "forbidden",
          },
        ],
        request_id: "req-5",
      }),
    );
    await expect(listAdminAssets("agents")).rejects.toMatchObject({
      code: "ASSET_RESPONSE_INVALID",
      message: "Shared asset response was invalid",
    });
  });

  test("maps explicit authentication failures and preserves aborts", async () => {
    mockedFetch.mockRejectedValueOnce(new AuthRequiredError());
    await expect(listAdminAssets("agents")).rejects.toMatchObject({
      status: 401,
      code: "AUTH_REQUIRED",
      message: "Authentication required",
    });

    const aborted = new DOMException("Aborted", "AbortError");
    mockedFetch.mockRejectedValueOnce(aborted);
    await expect(listAdminAssets("agents")).rejects.toBe(aborted);
  });

  test("covers the project version credential approval and binding mutation routes", async () => {
    const responses = [
      { data: [], request_id: "req-history" },
      { data: mcpVersion, request_id: "req-version" },
      { data: credentialVersion, request_id: "req-version" },
      {
        credential_id: asset.id,
        credential_version_id: versionId,
        migrated_count: 2,
        request_id: "req-migration",
      },
      { data: mcpVersion, request_id: "req-version" },
      { data: mcpVersion, request_id: "req-version" },
      { item: credential, request_id: "req-credential" },
      binding,
      { ...binding, version: 2 },
      { ...binding, enabled: false, version: 3 },
    ];
    for (const body of responses) {
      mockedFetch.mockResolvedValueOnce(jsonResponse(200, body));
    }

    await listProjectAssetVersions(PROJECT_ID, "agents", asset.id);
    await publishProjectAssetVersion(
      PROJECT_ID,
      "mcp-servers",
      asset.id,
      versionId,
      {
        expected_asset_version: 1,
      },
    );
    await replaceProjectCredential(PROJECT_ID, asset.id, {
      payload: { env: { TOKEN: "write-only" } },
      expected_credential_version: 1,
    });
    const migration = await migrateProjectCredentialGrants(
      PROJECT_ID,
      asset.id,
      { expected_credential_version: 2 },
    );
    await submitProjectMcpVersion(PROJECT_ID, asset.id, versionId, {
      expected_asset_version: 1,
    });
    await approveProjectMcpVersion(PROJECT_ID, asset.id, versionId, {
      credential_versions: { github: versionId },
      expected_asset_version: 1,
    });
    await revokeProjectCredential(PROJECT_ID, asset.id, {
      expected_credential_version: 1,
    });
    await enableProjectSystemBinding(PROJECT_ID, "agent", {
      asset_id: asset.id,
      version_id: versionId,
    });
    await upgradeProjectSystemBinding(PROJECT_ID, "agent", asset.id, {
      version_id: versionId,
      expected_binding_version: 1,
    });
    await disableProjectSystemBinding(PROJECT_ID, "agent", asset.id, {
      expected_binding_version: 2,
    });

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/projects/${PROJECT_ID}/agents/${asset.id}/versions`,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/publish`,
      `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}/replace`,
      `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}/migrate-grants`,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/submit-approval`,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/approve`,
      `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}/revoke`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings/${asset.id}/upgrade`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings/${asset.id}/disable`,
    ]);
    expect(migration.migrated_count).toBe(2);
    expect(mockedFetch.mock.calls[3]?.[1]?.body).toBe(
      JSON.stringify({ expected_credential_version: 2 }),
    );
  });

  test("creates typed project versions plus write-only project and system credentials", async () => {
    const responses = [
      { data: mcpVersion, request_id: "req-mcp" },
      { item: { ...credential, status: "active" }, request_id: "req-cred" },
      { item: { ...credential, status: "active" }, request_id: "req-cred" },
    ];
    for (const body of responses) {
      mockedFetch.mockResolvedValueOnce(jsonResponse(201, body));
    }

    const mcpInput = {
      description: "GitHub",
      transport: "stdio" as const,
      command: "github-mcp",
      args: [],
      url: null,
      env: {},
      headers: {},
      oauth: {},
      routing: {},
      tool_overrides: {},
      timeout_seconds: 30,
      credential_slots: [],
      expected_asset_version: 1,
    };
    const credentialInput = {
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    };

    await createProjectAssetVersion(
      PROJECT_ID,
      "mcp-servers",
      asset.id,
      mcpInput,
    );
    const credentialResult = await createProjectCredential(
      PROJECT_ID,
      credentialInput,
    );
    await createAdminCredential(credentialInput);

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions`,
      `/backend/api/projects/${PROJECT_ID}/credentials`,
      "/backend/api/admin/assets/credentials",
    ]);
    expect(mockedFetch.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify(credentialInput),
    );
    expect(credentialResult).not.toHaveProperty("payload");
  });
});
