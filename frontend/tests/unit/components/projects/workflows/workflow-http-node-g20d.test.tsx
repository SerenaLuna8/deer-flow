import { describe, expect, it, rs } from "@rstest/core";
import { ReactFlowProvider } from "@xyflow/react";
import { renderToStaticMarkup } from "react-dom/server";

import type { WorkflowFlowNode } from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import {
  HTTP_WORKFLOW_NODE_CONFIG_PANELS,
  HttpRequestNodeConfigPanel,
  applyHttpCurlDialog,
  buildHttpRequestNodeConfigUpdate,
  closeHttpCurlDialog,
  createHttpCurlDialogState,
  previewHttpCurlDialog,
  selectHttpEndpointConfig,
  selectHttpInjectionProfileAuth,
} from "@/components/projects/workflows/node-config/http";
import { HttpRequestWorkflowNode } from "@/components/projects/workflows/nodes/http-node-card";
import {
  WorkflowWorkbenchStoreProvider,
  type WorkflowWorkbenchStorePort,
  type WorkflowWorkbenchStoreSnapshot,
} from "@/components/projects/workflows/workbench/workbench-store-context";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  workflowNodeRegistryV1,
  type NodeCatalogEntry,
  type WorkflowHttpAuthoringV1,
} from "@/core/project-workflows/catalog";
import type { JsonValue } from "@/core/project-workflows/types";
import type { Capability } from "@/core/projects/types";

const NODE_ID = "10000000-0000-4000-8000-000000000007";

const completeHttpConfig = (): Record<string, JsonValue> => ({
  method: "GET",
  base_origin: "https://api.example.com",
  path_template: {
    version: 1,
    segments: [{ kind: "text", value: "/health" }],
  },
  query: [],
  headers: [],
  auth: { mode: "none" },
  body: { kind: "none" },
  timeout: { connect_ms: null, read_ms: 5000, write_ms: null },
  response: {
    mode: "json",
    accepted_statuses: [{ from: 200, to: 299 }],
    schema: null,
  },
});

function documentFor(
  config: Record<string, JsonValue>,
  withSlot = true,
  compatibleSlot = true,
): WorkflowPersistedDocumentV1 {
  return {
    spec: {
      schema_version: 1,
      nodes: [
        {
          id: NODE_ID,
          type: "http_request",
          type_version: 1,
          scope: { kind: "root" },
          custom_label: null,
          description: null,
          input_bindings: {},
          execution_policy: {
            retry: { mode: "none" },
            on_error: { mode: "fail_workflow" },
          },
          config,
        },
      ],
      credential_slots: withSlot
        ? [
            {
              id: "http_auth_slot",
              name: "HTTP authorization",
              purpose: "http_auth",
              payload_schema: {
                type: "object",
                ...(compatibleSlot
                  ? {
                      properties: { value: { type: "string" } },
                      required: ["value"],
                    }
                  : {}),
                additionalProperties: false,
              },
              required: true,
            },
          ]
        : [],
    },
    canvas: { schema_version: 1 },
  };
}

const HTTP_AUTHORING: WorkflowHttpAuthoringV1 = {
  endpoints: [
    {
      id: "public-api",
      origin: "https://api.example.com",
      allowed_methods: ["GET", "HEAD", "POST"],
      write_idempotency: "server_derived_key",
      injection_profiles: [
        {
          id: "api-key-v1",
          scheme: "api_key",
          target_header: "x-api-key",
          credential_payload_contract: "api_key_v1",
        },
      ],
    },
    {
      id: "read-only-api",
      origin: "https://readonly.example.com",
      allowed_methods: ["GET"],
      write_idempotency: "none",
      injection_profiles: [],
    },
  ],
};

function catalogEntry(
  disabled = false,
  authoringAvailable = true,
): NodeCatalogEntry {
  const definition = workflowNodeRegistryV1.find(
    (candidate) => candidate.type === "http_request",
  );
  if (!definition) throw new Error("HTTP registry entry missing");
  return {
    definition,
    availability: disabled
      ? { state: "disabled", reason_code: "WORKFLOW_HTTP_DISABLED" }
      : { state: "enabled" },
    public_limits: {
      max_timeout_ms: 30_000,
      max_http_request_bytes: 65_536,
      max_http_response_bytes: 1_048_576,
    },
    ...(authoringAvailable ? { http_authoring: HTTP_AUTHORING } : {}),
  };
}

function storeFor(
  document: WorkflowPersistedDocumentV1,
): WorkflowWorkbenchStorePort {
  const snapshot: WorkflowWorkbenchStoreSnapshot = {
    baseline: document,
    current: document,
    dirty: false,
    history: { past: [], future: [], epoch: 0 },
    editorSession: {
      schema_version: 1,
      viewport: { x: 0, y: 0, zoom: 1 },
      selection: { node_ids: [NODE_ID], edge_ids: [] },
      inspector: {
        open: true,
        node_id: NODE_ID,
        tab: "settings",
        width_px: 480,
        expanded_section_ids: [],
        scroll_top: 0,
      },
      palette: { open: false, anchor: null },
      interaction: { kind: "idle" },
    },
    runtimeProjection: null,
    validationIssues: [],
  };
  return {
    dispatch: rs.fn(() => ({ applied: true, issues: [] })),
    getState: () => snapshot,
    redo: rs.fn(() => false),
    setEditorSession: rs.fn(() => true),
    subscribe: () => () => undefined,
    undo: rs.fn(() => false),
  };
}

function renderPanel({
  capabilities = [
    "workflow.read",
    "workflow.edit",
    "workflow.http.use",
    "workflow.http.write",
  ],
  config = completeHttpConfig(),
  disabled = false,
  readOnly = false,
  catalogDisabled = false,
  authoringAvailable = true,
  withSlot = true,
  compatibleSlot = true,
}: {
  capabilities?: readonly Capability[];
  config?: Record<string, JsonValue>;
  disabled?: boolean;
  readOnly?: boolean;
  catalogDisabled?: boolean;
  authoringAvailable?: boolean;
  withSlot?: boolean;
  compatibleSlot?: boolean;
} = {}): string {
  const document = documentFor(config, withSlot, compatibleSlot);
  const node = document.spec.nodes?.[0];
  if (!node) throw new Error("HTTP node fixture missing");
  return renderToStaticMarkup(
    <WorkflowWorkbenchStoreProvider store={storeFor(document)}>
      <HttpRequestNodeConfigPanel
        capabilities={capabilities}
        catalogEntry={catalogEntry(catalogDisabled, authoringAvailable)}
        disabled={disabled}
        document={document}
        locale="zh-CN"
        node={node}
        nodeId={NODE_ID}
        readOnly={readOnly}
      />
    </WorkflowWorkbenchStoreProvider>,
  );
}

describe("G20-D HTTP Request node", () => {
  it("freezes the dedicated panel and card exports", () => {
    expect(HTTP_WORKFLOW_NODE_CONFIG_PANELS).toEqual({
      http_request: HttpRequestNodeConfigPanel,
    });
    expect(HttpRequestWorkflowNode).toBeTypeOf("function");
  });

  it("normalizes cURL into a secret-free diff before explicit apply", () => {
    const source =
      "curl -X POST 'https://api.example.com/v1/items?q=hello%20world' " +
      "-H 'Content-Type: application/json' -H 'X-Trace: safe' " +
      '--data-raw \'{"name":"Ada"}\'';
    const state = previewHttpCurlDialog(
      createHttpCurlDialogState(source),
      completeHttpConfig(),
      true,
      null,
      HTTP_AUTHORING,
    );

    expect(state.error).toBeNull();
    expect(state.preview?.config).toMatchObject({
      method: "POST",
      base_origin: "https://api.example.com",
      path_template: {
        version: 1,
        segments: [{ kind: "text", value: "/v1/items" }],
      },
      query: [
        {
          id: "curl_query_1",
          name: "q",
          value: { kind: "literal", value: "hello world" },
        },
      ],
      headers: [
        {
          id: "curl_header_1",
          name: "x-trace",
          value: { kind: "literal", value: "safe" },
        },
      ],
      auth: { mode: "none" },
      body: {
        kind: "json",
        template: {
          version: 1,
          template: { name: "Ada" },
          bindings: {},
        },
      },
    });
    expect(JSON.stringify(state.preview)).not.toContain(source);
    expect(state.preview?.changes.length).toBeGreaterThan(0);

    const applied = applyHttpCurlDialog(
      state,
      completeHttpConfig(),
      true,
      null,
      HTTP_AUTHORING,
    );
    expect(applied.config?.method).toBe("POST");
    expect(applied.state).toEqual(createHttpCurlDialogState());
    expect(JSON.stringify(applied)).not.toContain(source);
  });

  it("keeps rejected cURL local and clears it on close without network", () => {
    let fetchCalls = 0;
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (() => {
      fetchCalls += 1;
      throw new Error("network must not be used");
    }) as typeof fetch;
    try {
      const source =
        "curl -H 'Authorization: Bearer super-secret' https://api.example.com";
      const rejected = previewHttpCurlDialog(
        createHttpCurlDialogState(source),
        completeHttpConfig(),
        true,
        null,
        HTTP_AUTHORING,
      );
      expect(rejected).toMatchObject({
        source,
        preview: null,
        error: "unsafe_or_invalid",
      });
      expect(fetchCalls).toBe(0);
      expect(closeHttpCurlDialog()).toEqual(createHttpCurlDialogState());
      expect(JSON.stringify(closeHttpCurlDialog())).not.toContain(
        "super-secret",
      );
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("fails closed when cURL requests a write without workflow.http.write", () => {
    const state = previewHttpCurlDialog(
      createHttpCurlDialogState(
        "curl -X DELETE https://api.example.com/v1/items/1",
      ),
      completeHttpConfig(),
      false,
      null,
      HTTP_AUTHORING,
    );
    expect(state.preview).toBeNull();
    expect(state.error).toBe("write_capability_required");
    expect(
      applyHttpCurlDialog(
        state,
        completeHttpConfig(),
        false,
        null,
        HTTP_AUTHORING,
      ).config,
    ).toBeNull();
  });

  it.each([
    {
      label: "password query",
      source: "curl 'https://api.example.com/items?password=secret'",
    },
    {
      label: "generic key query",
      source: "curl 'https://api.example.com/items?key=secret'",
    },
    {
      label: "auth-like header",
      source: "curl -H 'X-Auth: secret' https://api.example.com/items",
    },
    {
      label: "shell-like body variable",
      source: "curl --data '$HOME' https://api.example.com/items",
    },
  ])(
    "rejects additional secret-like or shell-like cURL fields: $label",
    ({ source }) => {
      const state = previewHttpCurlDialog(
        createHttpCurlDialogState(source),
        completeHttpConfig(),
        true,
        null,
        HTTP_AUTHORING,
      );
      expect(state.preview).toBeNull();
      expect(state.error).toBe("unsafe_or_invalid");
    },
  );

  it("rejects a normalized cURL request above the public byte limit", () => {
    const state = previewHttpCurlDialog(
      createHttpCurlDialogState(
        "curl --data-raw 'bounded body' https://api.example.com/items",
      ),
      completeHttpConfig(),
      true,
      16,
      HTTP_AUTHORING,
    );
    expect(state.preview).toBeNull();
    expect(state.error).toBe("request_limit_exceeded");
  });

  it("rejects cURL origins and methods outside the safe Catalog authority", () => {
    const unknownOrigin = previewHttpCurlDialog(
      createHttpCurlDialogState("curl https://unknown.example.com/items"),
      completeHttpConfig(),
      true,
      65_536,
      HTTP_AUTHORING,
    );
    expect(unknownOrigin.preview).toBeNull();
    expect(unknownOrigin.error).toBe("endpoint_not_available");

    const disallowedMethod = previewHttpCurlDialog(
      createHttpCurlDialogState(
        "curl -X DELETE https://api.example.com/items/1",
      ),
      completeHttpConfig(),
      true,
      65_536,
      HTTP_AUTHORING,
    );
    expect(disallowedMethod.preview).toBeNull();
    expect(disallowedMethod.error).toBe("endpoint_not_available");
  });

  it("writes the exact Catalog origin for an explicit default HTTPS port", () => {
    const authoring: WorkflowHttpAuthoringV1 = {
      endpoints: [
        {
          ...HTTP_AUTHORING.endpoints[0]!,
          origin: "https://api.example.com:443",
        },
      ],
    };
    const state = previewHttpCurlDialog(
      createHttpCurlDialogState("curl https://api.example.com:443/v1/items"),
      completeHttpConfig(),
      true,
      65_536,
      authoring,
    );

    expect(state.error).toBeNull();
    expect(state.preview?.config.base_origin).toBe(
      "https://api.example.com:443",
    );
  });

  it("revalidates current capability and Catalog authority at cURL Apply", () => {
    const previewed = previewHttpCurlDialog(
      createHttpCurlDialogState(
        "curl -X POST https://api.example.com/v1/items",
      ),
      completeHttpConfig(),
      true,
      65_536,
      HTTP_AUTHORING,
    );
    expect(previewed.preview).not.toBeNull();

    expect(
      applyHttpCurlDialog(
        previewed,
        completeHttpConfig(),
        false,
        65_536,
        HTTP_AUTHORING,
      ).config,
    ).toBeNull();
    expect(
      applyHttpCurlDialog(previewed, completeHttpConfig(), true, 65_536, {
        endpoints: [HTTP_AUTHORING.endpoints[1]!],
      }).config,
    ).toBeNull();
  });

  it("derives endpoint and profile mutations only from the safe Catalog projection", () => {
    const endpoint = HTTP_AUTHORING.endpoints[0];
    if (endpoint === undefined)
      throw new Error("HTTP endpoint fixture missing");
    const endpointPatch = selectHttpEndpointConfig(
      {
        ...completeHttpConfig(),
        method: "DELETE",
        base_origin: "https://legacy.example.com",
        auth: {
          mode: "endpoint_profile",
          injection_profile_id: "legacy-profile",
          credential_slot_id: "http_auth_slot",
        },
        body: { kind: "raw_text" },
      },
      endpoint,
      false,
    );
    expect(endpointPatch).toEqual({
      base_origin: "https://api.example.com",
      method: "GET",
      auth: { mode: "none" },
      body: { kind: "none" },
    });

    expect(
      selectHttpInjectionProfileAuth(
        "api-key-v1",
        "http_auth_slot",
        endpoint.injection_profiles,
        documentFor(completeHttpConfig()),
      ),
    ).toEqual({
      mode: "endpoint_profile",
      injection_profile_id: "api-key-v1",
      credential_slot_id: "http_auth_slot",
    });
    expect(
      selectHttpInjectionProfileAuth(
        "forged-profile",
        "http_auth_slot",
        endpoint.injection_profiles,
        documentFor(completeHttpConfig()),
      ),
    ).toBeNull();
  });

  it("drops unknown authority fields from config updates", () => {
    const document = documentFor({
      ...completeHttpConfig(),
      runtime_profile: "must-be-dropped",
      credential_id: "must-be-dropped",
    });
    const node = document.spec.nodes?.[0];
    if (!node) throw new Error("HTTP node fixture missing");
    const command = buildHttpRequestNodeConfigUpdate(node, { method: "HEAD" });
    expect(command).toMatchObject({
      type: "update_node_config",
      node_id: NODE_ID,
      config: { method: "HEAD" },
    });
    expect(command.config).not.toHaveProperty("runtime_profile");
    expect(command.config).not.toHaveProperty("credential_id");
  });

  it("renders partial Draft issues, TLS locks, public limits, and no private values", () => {
    const html = renderPanel({
      config: {
        method: "GET",
        base_origin: "https://api.example.com",
        runtime_profile: "must-not-render",
        secret: "must-not-render",
      },
    });
    expect(html).toContain("HTTP 请求配置");
    expect(html).toContain("TLS 证书验证：始终开启");
    expect(html).toContain("30000 ms");
    expect(html).toContain("65536 bytes");
    expect(html).toContain("1048576 bytes");
    expect(html).toContain('role="alert"');
    expect(html).not.toContain("must-not-render");
    expect(html).not.toContain("runtime_profile");
  });

  it("selects only declared opaque slots and distinguishes missing declaration", () => {
    const configured = {
      ...completeHttpConfig(),
      auth: {
        mode: "endpoint_profile",
        injection_profile_id: "api-key-v1",
        credential_slot_id: "http_auth_slot",
      },
    };
    const declared = renderPanel({ config: configured });
    expect(declared).toContain("http_auth_slot");
    expect(declared).toContain("Slot 声明就绪");
    expect(declared).toContain("x-api-key");
    expect(declared).toContain("api_key_v1");
    expect(declared).toContain("Grant 由 Published Version 独立授权");
    expect(declared).not.toContain("Credential ID");

    const missing = renderPanel({ config: configured, withSlot: false });
    expect(missing).toContain("Slot 声明缺失或 payload contract 不兼容");
    expect(missing).not.toContain("HTTP authorization");

    const incompatible = renderPanel({
      compatibleSlot: false,
      config: configured,
    });
    expect(incompatible).toContain("http_auth_slot（当前值不可用）");
    expect(incompatible).toContain("Slot 声明缺失或 payload contract 不兼容");
  });

  it("uses selectors only and preserves unavailable legacy endpoint/profile values read-only", () => {
    const html = renderPanel({
      config: {
        ...completeHttpConfig(),
        base_origin: "https://legacy.example.com",
        auth: {
          mode: "endpoint_profile",
          injection_profile_id: "legacy-profile",
          credential_slot_id: "http_auth_slot",
        },
      },
    });

    expect(html).toContain('aria-label="HTTP endpoint policy"');
    expect(html).toContain("https://legacy.example.com（当前值不可用）");
    expect(html).toContain("legacy-profile（当前值不可用）");
    expect(html).not.toContain('aria-label="HTTP base origin"');
  });

  it("fails closed when the HTTP authoring projection is missing", () => {
    const html = renderPanel({ authoringAvailable: false });
    expect(html).toMatch(/<fieldset[^>]*disabled/);
    expect(html).toContain("HTTP authoring authority 不可用");
  });

  it("disables write methods without workflow.http.write", () => {
    const html = renderPanel({
      capabilities: ["workflow.read", "workflow.edit", "workflow.http.use"],
    });
    expect(html).toContain("缺少 workflow.http.write");
    expect(html).toMatch(/<option[^>]*disabled[^>]*value="POST"/);
    expect(html).not.toMatch(/<option[^>]*value="DELETE"/);
  });

  it.each([
    ["readOnly", { readOnly: true }],
    ["disabled", { disabled: true }],
    [
      "missing edit/use capability",
      { capabilities: ["workflow.read"] as const },
    ],
    ["catalog disabled", { catalogDisabled: true }],
  ] as const)("fails closed for %s", (_label, options) => {
    const html = renderPanel(options);
    expect(html).toMatch(/<fieldset[^>]*disabled/);
    expect(html).toContain('aria-disabled="true"');
  });

  it("renders only closed safe HTTP card metadata", () => {
    const data: WorkflowFlowNode["data"] = {
      nodeId: NODE_ID,
      nodeKind: "http_request",
      originalType: "http_request",
      title: "HTTP 请求",
      supportState: "supported",
      statusLabel: "状态：可用",
      availabilityReason: null,
      readOnly: false,
      disabled: false,
      inputPorts: [],
      outputPorts: [],
      focusedPortId: null,
      portSignature: "safe-signature",
      httpMethod: "POST",
      httpPolicyState: "approved",
      httpCredentialSlotState: "missing",
      base_origin: "must-not-render",
      path_template: "must-not-render",
      headers: "must-not-render",
      body: "must-not-render",
      raw_curl: "must-not-render",
      secret: "must-not-render",
    };
    const html = renderToStaticMarkup(
      <ReactFlowProvider>
        <HttpRequestWorkflowNode
          data={data}
          deletable
          draggable
          dragging={false}
          id={NODE_ID}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
          selectable
          selected={false}
          type="http_request"
          zIndex={0}
        />
      </ReactFlowProvider>,
    );
    expect(html).toContain('data-http-node-card="true"');
    expect(html).toContain("POST");
    expect(html).toContain("Endpoint policy 已批准");
    expect(html).toContain("Slot 声明缺失");
    expect(html).toContain("状态：可用");
    expect(html).not.toContain("must-not-render");
  });
});
