import { describe, expect, test } from "@rstest/core";

import {
  buildProjectMcpFormSubmission,
  projectMcpCredentialCopy,
  projectMcpSecretInputName,
  projectMcpSecretSlotsForSubmission,
} from "@/components/projects/assets/project-mcp-form-dialog";
import type { ProjectMcpEditableConfigurationResponse } from "@/core/shared-assets";

describe("Project MCP form language", () => {
  test("explains optional access credentials without exposing slot terminology", () => {
    const copy = projectMcpCredentialCopy();
    const { valueLabel, ...staticCopy } = copy;

    expect(staticCopy).toEqual({
      description:
        "填写 MCP 服务地址；如服务需要 API Key 或 Token，可在下方配置访问凭证。凭证仅由当前 Project 加密保存。",
      sectionTitle: "访问凭证（可选）",
      listHelp: "每行对应一个请求参数，可同时配置请求头和 URL 查询参数。",
      editValueHelp:
        "仅当参数结构、Transport 和服务地址来源均未变化时，留空才会保留已有值；否则请重新填写全部凭证值。",
      targetLabel: "发送位置",
      targetOptions: {
        headers: "请求头（Header）",
        query: "URL 查询参数（Query）",
      },
      addLabel: "添加凭证参数",
      fields: {
        headers: {
          itemLabel: "请求头",
          nameLabel: "请求头名称",
          namePlaceholder: "Authorization",
          valueLabel: "凭证值",
        },
        query: {
          itemLabel: "查询参数",
          nameLabel: "查询参数名称",
          namePlaceholder: "api_key",
          valueLabel: "凭证值",
        },
      },
    });
    expect(valueLabel("Authorization")).toBe("Authorization 的凭证值");
    expect(JSON.stringify(copy)).not.toMatch(/秘密槽位|注入位置|槽位名/u);
  });
});

describe("Project MCP form secret-slot preservation", () => {
  test("editing a multi-slot definition preserves every exact slot", () => {
    const existing: Parameters<typeof projectMcpSecretSlotsForSubmission>[0] = [
      {
        name: "primary",
        purpose: "Primary authentication",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
      {
        name: "tenant",
        purpose: "Tenant routing",
        payload_schema: { query: ["tenant_token"] },
        required: true,
      },
    ];
    const editedFirstSlot: Parameters<
      typeof projectMcpSecretSlotsForSubmission
    >[1] = [
      {
        name: "primary-edited",
        purpose: "Edited in the single-slot control",
        payload_schema: { headers: ["X-Api-Key"] },
        required: true,
      },
    ];

    expect(
      projectMcpSecretSlotsForSubmission(existing, editedFirstSlot),
    ).toEqual(existing);
  });

  test("new and single-slot definitions use the visible editor value", () => {
    const edited = [
      {
        name: "auth",
        purpose: "MCP request header secrets",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
    ];

    expect(projectMcpSecretSlotsForSubmission([], edited)).toEqual(edited);
    expect(projectMcpSecretSlotsForSubmission([edited[0]!], edited)).toEqual(
      edited,
    );
  });

  test("a single-slot editor preserves hidden optionality and purpose", () => {
    const existing: Parameters<typeof projectMcpSecretSlotsForSubmission>[0] = [
      {
        name: "tenant",
        purpose: "Optional tenant-specific routing",
        payload_schema: { query: ["tenant_token"] },
        required: false,
      },
    ];
    const editorProjection: Parameters<
      typeof projectMcpSecretSlotsForSubmission
    >[1] = [
      {
        name: "tenant",
        purpose: "MCP query parameter secrets",
        payload_schema: {
          headers: ["X-Tenant"],
          query: ["tenant_token"],
        },
        required: true,
      },
    ];

    expect(
      projectMcpSecretSlotsForSubmission(existing, editorProjection),
    ).toEqual([
      {
        name: "tenant",
        purpose: "Optional tenant-specific routing",
        payload_schema: {
          headers: ["X-Tenant"],
          query: ["tenant_token"],
        },
        required: false,
      },
    ]);
  });

  test("local validation failure consumes write-only secret values first", () => {
    const form = new FormData();
    form.set("url", "https://mcp.example.test/tools?invalid=true");
    form.set(projectMcpSecretInputName(0), "temporary-secret");
    let cleared = false;

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        fields: [{ id: 0, target: "headers", name: "Authorization" }],
        clearSecretValues: () => {
          cleared = true;
        },
      }),
    ).toThrow("MCP URL 必须是没有 query 或 fragment 的有效地址。");
    expect(cleared).toBe(true);
  });

  test("rejects request header names that differ only by case", () => {
    const form = new FormData();
    form.set("url", "http://127.0.0.1:65535/mcp");

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        fields: [
          { id: 3, target: "headers", name: "Authorization" },
          { id: 8, target: "headers", name: "authorization" },
        ],
        clearSecretValues: () => undefined,
      }),
    ).toThrow("请求头名称不能重复。");
  });

  test.each(["Host", "Content-Length", "bad header"])(
    "rejects unsafe request header name %s before submission",
    (name) => {
      const form = new FormData();
      form.set("url", "http://127.0.0.1:65535/mcp");
      form.set(projectMcpSecretInputName(3), "temporary-secret");
      let cleared = false;

      expect(() =>
        buildProjectMcpFormSubmission({
          form,
          fields: [{ id: 3, target: "headers", name }],
          clearSecretValues: () => {
            cleared = true;
          },
        }),
      ).toThrow(/请求头/u);
      expect(cleared).toBe(true);
    },
  );

  test.each(["api key", "api/key", "x".repeat(129)])(
    "rejects invalid Query parameter name %s before submission",
    (name) => {
      const form = new FormData();
      form.set("url", "http://127.0.0.1:65535/mcp");

      expect(() =>
        buildProjectMcpFormSubmission({
          form,
          fields: [{ id: 4, target: "query", name }],
          clearSecretValues: () => undefined,
        }),
      ).toThrow(/查询参数名称/u);
    },
  );

  test("scopes names by target and keeps Query names case-sensitive", () => {
    const form = new FormData();
    form.set("url", "http://127.0.0.1:65535/mcp");

    const submission = buildProjectMcpFormSubmission({
      form,
      fields: [
        { id: 1, target: "headers", name: "token" },
        { id: 5, target: "query", name: "token" },
        { id: 9, target: "query", name: "Token" },
      ],
      clearSecretValues: () => undefined,
    });

    expect(submission.input.secret_slots?.[0]?.payload_schema).toEqual({
      headers: ["token"],
      query: ["token", "Token"],
    });
  });

  test("rejects exact duplicate Query parameter names", () => {
    const form = new FormData();
    form.set("url", "http://127.0.0.1:65535/mcp");

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        fields: [
          { id: 6, target: "query", name: "api_key" },
          { id: 10, target: "query", name: "api_key" },
        ],
        clearSecretValues: () => undefined,
      }),
    ).toThrow("查询参数名称不能重复。");
  });

  test("submits Header and Query rows in one atomic write-only slot", () => {
    const form = new FormData();
    form.set("display_name", "Example MCP");
    form.set("slug", "example-mcp");
    form.set("url", "http://127.0.0.1:65535/mcp");
    form.set(projectMcpSecretInputName(7), "temporary-bearer");
    form.set(projectMcpSecretInputName(11), "temporary-api-key");
    let cleared = false;

    const submission = buildProjectMcpFormSubmission({
      form,
      fields: [
        { id: 7, target: "headers", name: "Authorization" },
        { id: 11, target: "query", name: "api_key" },
      ],
      clearSecretValues: () => {
        cleared = true;
      },
    });

    expect(submission.secret).toEqual({
      slotName: "auth",
      payload: {
        headers: {
          Authorization: "temporary-bearer",
        },
        query: {
          api_key: "temporary-api-key",
        },
      },
    });
    expect(submission.input.secret_slots ?? []).toEqual([
      {
        name: "auth",
        purpose: "MCP request credentials",
        payload_schema: {
          headers: ["Authorization"],
          query: ["api_key"],
        },
        required: true,
      },
    ]);
    expect(cleared).toBe(true);
  });

  test("rejects partially filled credential rows and consumes every value", () => {
    const form = new FormData();
    form.set("url", "http://127.0.0.1:65535/mcp");
    form.set(projectMcpSecretInputName(2), "temporary-bearer");
    let cleared = false;

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        fields: [
          { id: 2, target: "headers", name: "Authorization" },
          { id: 9, target: "headers", name: "X-API-Key" },
        ],
        clearSecretValues: () => {
          cleared = true;
        },
      }),
    ).toThrow("请填写全部凭证值；若暂不配置，请全部留空。");
    expect(cleared).toBe(true);
  });

  test("preserves an existing internal slot name while editing dynamic rows", () => {
    const form = new FormData();
    form.set("url", "http://127.0.0.1:65535/mcp");
    form.set(projectMcpSecretInputName(4), "tenant-secret");
    const configuration = {
      item: { version: 3 },
      version: {
        secret_slots: [
          {
            name: "tenant",
            purpose: "Tenant authentication",
            payload_schema: { query: ["tenant_token"] },
            required: true,
          },
        ],
        definition: {
          secret_slots: [
            {
              name: "tenant",
              purpose: "Tenant authentication",
              payload_schema: { query: ["tenant_token"] },
              required: true,
            },
          ],
        },
      },
    } as unknown as ProjectMcpEditableConfigurationResponse;

    const submission = buildProjectMcpFormSubmission({
      form,
      configuration,
      fields: [{ id: 4, target: "query", name: "tenant_token" }],
      clearSecretValues: () => undefined,
    });

    expect(submission.secret?.slotName).toBe("tenant");
    expect(submission.input.secret_slots?.[0]?.name).toBe("tenant");
  });

  test("requires every credential value after an existing schema changes", () => {
    const form = new FormData();
    form.set("url", "http://127.0.0.1:65535/mcp");
    const configuration = {
      item: { version: 3 },
      version: {
        secret_slots: [
          {
            name: "auth",
            purpose: "Authentication",
            payload_schema: { headers: ["Authorization"] },
            required: true,
          },
        ],
        definition: {
          transport: "http",
          url: "http://127.0.0.1:65535/mcp",
          secret_slots: [
            {
              name: "auth",
              purpose: "Authentication",
              payload_schema: { headers: ["Authorization"] },
              required: true,
            },
          ],
        },
      },
    } as unknown as ProjectMcpEditableConfigurationResponse;

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        configuration,
        fields: [{ id: 0, target: "query", name: "api_key" }],
        clearSecretValues: () => undefined,
      }),
    ).toThrow("凭证参数已发生变化，请重新填写全部凭证值。");
  });

  test("keeps existing credential values when its schema and endpoint origin are unchanged", () => {
    const form = new FormData();
    form.set("transport", "http");
    form.set("url", "http://127.0.0.1:65535/another-path");
    const configuration = {
      item: { version: 3 },
      version: {
        secret_slots: [
          {
            name: "auth",
            purpose: "Authentication",
            payload_schema: {
              headers: ["Authorization"],
              query: ["api_key"],
            },
            required: true,
          },
        ],
        definition: {
          transport: "http",
          url: "http://127.0.0.1:65535/mcp",
          secret_slots: [
            {
              name: "auth",
              purpose: "Authentication",
              payload_schema: {
                headers: ["Authorization"],
                query: ["api_key"],
              },
              required: true,
            },
          ],
        },
      },
    } as unknown as ProjectMcpEditableConfigurationResponse;

    const submission = buildProjectMcpFormSubmission({
      form,
      configuration,
      fields: [
        { id: 0, target: "headers", name: "Authorization" },
        { id: 1, target: "query", name: "api_key" },
      ],
      clearSecretValues: () => undefined,
    });

    expect(submission.secret).toBeNull();
  });

  test("requires credential values when the endpoint origin changes", () => {
    const form = new FormData();
    form.set("transport", "http");
    form.set("url", "http://127.0.0.1:65534/mcp");
    const configuration = {
      item: { version: 3 },
      version: {
        secret_slots: [
          {
            name: "auth",
            purpose: "Authentication",
            payload_schema: { headers: ["Authorization"] },
            required: true,
          },
        ],
        definition: {
          transport: "http",
          url: "http://127.0.0.1:65535/mcp",
          secret_slots: [
            {
              name: "auth",
              purpose: "Authentication",
              payload_schema: { headers: ["Authorization"] },
              required: true,
            },
          ],
        },
      },
    } as unknown as ProjectMcpEditableConfigurationResponse;

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        configuration,
        fields: [{ id: 0, target: "headers", name: "Authorization" }],
        clearSecretValues: () => undefined,
      }),
    ).toThrow("MCP 服务地址或 Transport 已变化，请重新填写全部凭证值。");
  });
});
