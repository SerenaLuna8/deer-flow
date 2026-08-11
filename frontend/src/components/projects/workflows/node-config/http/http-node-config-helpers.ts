import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import { serializeCanonicalJsonValue } from "@/core/project-workflows/canonical";
import {
  workflowHttpAuthoringOriginV1Schema,
  type WorkflowHttpAuthoringV1,
  type WorkflowHttpEndpointAuthoringV1,
  type WorkflowHttpInjectionProfileAuthoringV1,
} from "@/core/project-workflows/catalog";
import { parseWorkflowHttpCurl } from "@/core/project-workflows/curl-parser";
import {
  httpRequestNodeConfigV1Schema,
  jsonValueSchema,
  type JsonValue,
} from "@/core/project-workflows/types";

type HttpDraftNode = WorkflowNodeConfigPanelProps["node"];

const HTTP_CONFIG_FIELDS = [
  "method",
  "base_origin",
  "path_template",
  "query",
  "headers",
  "auth",
  "body",
  "timeout",
  "response",
] as const;

const HTTP_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] as const;

export type HttpRequestMethod = (typeof HTTP_METHODS)[number];

const HTTP_WRITE_METHODS = new Set<HttpRequestMethod>([
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
]);

const SAFE_SLOT_ID = /^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$/;

type HttpCredentialPayloadContract =
  WorkflowHttpInjectionProfileAuthoringV1["credential_payload_contract"];

const HTTP_CREDENTIAL_PAYLOAD_SCHEMAS = {
  bearer_token_v1: {
    type: "object",
    properties: { token: { type: "string" } },
    required: ["token"],
    additionalProperties: false,
  },
  basic_auth_v1: {
    type: "object",
    properties: {
      username: { type: "string" },
      password: { type: "string" },
    },
    required: ["username", "password"],
    additionalProperties: false,
  },
  api_key_v1: {
    type: "object",
    properties: { value: { type: "string" } },
    required: ["value"],
    additionalProperties: false,
  },
} as const satisfies Record<HttpCredentialPayloadContract, JsonValue>;

const FORBIDDEN_HEADER_NAMES = new Set([
  "authorization",
  "connection",
  "content-length",
  "cookie",
  "forwarded",
  "host",
  "idempotency-key",
  "proxy-authenticate",
  "proxy-authorization",
  "set-cookie",
  "transfer-encoding",
]);

const HEADER_NAME = /^[a-z0-9!#$%&'*+.^_`|~-]{1,128}$/;
const SECRET_NAME =
  /(?:^|[-_])(api[-_]?key|auth|authorization|bearer|credential|key|password|passwd|secret|token|access[-_]?token)(?:$|[-_])/i;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

const jsonValueOrNull = (value: unknown): JsonValue => {
  const parsed = jsonValueSchema.safeParse(value);
  return parsed.success ? structuredClone(parsed.data) : null;
};

export const httpConfigRecord = (
  node: HttpDraftNode,
): Record<string, unknown> => (isRecord(node.config) ? node.config : {});

export function buildHttpRequestNodeConfigUpdate(
  node: HttpDraftNode,
  patch: Record<string, unknown>,
) {
  const merged = { ...httpConfigRecord(node), ...patch };
  const config: Record<string, JsonValue> = {};
  for (const field of HTTP_CONFIG_FIELDS) {
    if (!Object.hasOwn(merged, field)) continue;
    const parsed = jsonValueSchema.safeParse(merged[field]);
    if (parsed.success) config[field] = structuredClone(parsed.data);
  }
  return {
    type: "update_node_config" as const,
    node_id: typeof node.id === "string" ? node.id : "",
    config,
  };
}

export const httpMethodIsWrite = (method: unknown): boolean =>
  HTTP_WRITE_METHODS.has(method as HttpRequestMethod);

export const safeHttpMethod = (method: unknown): HttpRequestMethod =>
  HTTP_METHODS.includes(method as HttpRequestMethod)
    ? (method as HttpRequestMethod)
    : "GET";

export const selectHttpEndpointConfig = (
  config: Record<string, unknown>,
  endpoint: WorkflowHttpEndpointAuthoringV1,
  canWrite: boolean,
): Record<string, JsonValue> | null => {
  const selectableMethods = endpoint.allowed_methods.filter(
    (candidate) => canWrite || !httpMethodIsWrite(candidate),
  );
  if (selectableMethods.length === 0) return null;
  const currentMethod = safeHttpMethod(config.method);
  const method = selectableMethods.includes(currentMethod)
    ? currentMethod
    : selectableMethods[0]!;
  const patch: Record<string, JsonValue> = {
    base_origin: endpoint.origin,
    method,
  };
  const auth = isRecord(config.auth) ? config.auth : {};
  if (
    auth.mode === "endpoint_profile" &&
    !endpoint.injection_profiles.some(
      (profile) => profile.id === auth.injection_profile_id,
    )
  ) {
    patch.auth = { mode: "none" };
  }
  if (
    (method === "GET" || method === "HEAD") &&
    (!isRecord(config.body) || config.body.kind !== "none")
  ) {
    patch.body = { kind: "none" };
  }
  return patch;
};

export const selectHttpInjectionProfileAuth = (
  profileId: string,
  currentSlotId: string,
  profiles: readonly WorkflowHttpInjectionProfileAuthoringV1[],
  document: WorkflowNodeConfigPanelProps["document"],
): Record<string, JsonValue> | null => {
  const profile = profiles.find((candidate) => candidate.id === profileId);
  if (profile === undefined) return null;
  const declaredSlotIds = httpCredentialSlotIds(
    document,
    profile.credential_payload_contract,
  );
  const credentialSlotId = declaredSlotIds.includes(currentSlotId)
    ? currentSlotId
    : declaredSlotIds[0];
  if (credentialSlotId === undefined) return null;
  return {
    mode: "endpoint_profile",
    injection_profile_id: profileId,
    credential_slot_id: credentialSlotId,
  };
};

export const httpHeaderNameIsSafe = (value: unknown): boolean => {
  if (typeof value !== "string") return false;
  const name = value.trim().toLowerCase();
  return (
    HEADER_NAME.test(name) &&
    !FORBIDDEN_HEADER_NAMES.has(name) &&
    !name.startsWith("proxy-") &&
    !name.startsWith("x-forwarded-") &&
    !SECRET_NAME.test(name)
  );
};

export const httpQueryNameIsSafe = (value: unknown): boolean =>
  typeof value === "string" &&
  value.length > 0 &&
  value.length <= 128 &&
  !/[\r\n\0]/.test(value) &&
  !SECRET_NAME.test(value);

export const httpBaseOriginIsSafe = (value: unknown): boolean => {
  return workflowHttpAuthoringOriginV1Schema.safeParse(value).success;
};

export const httpPathTemplateIsSafe = (value: unknown): boolean => {
  if (!isRecord(value) || value.version !== 1) return false;
  if (!Array.isArray(value.segments) || value.segments.length === 0) {
    return false;
  }
  const first = value.segments[0];
  return (
    isRecord(first) &&
    first.kind === "text" &&
    typeof first.value === "string" &&
    first.value.startsWith("/")
  );
};

export const httpCredentialSlotIds = (
  document: WorkflowNodeConfigPanelProps["document"],
  contract: HttpCredentialPayloadContract,
): string[] => {
  const slots = Array.isArray(document.spec.credential_slots)
    ? document.spec.credential_slots
    : [];
  return [
    ...new Set(
      slots.flatMap((slot) => {
        if (
          typeof slot.id !== "string" ||
          !SAFE_SLOT_ID.test(slot.id) ||
          slot.purpose !== "http_auth" ||
          slot.required !== true
        ) {
          return [];
        }
        const payloadSchema = jsonValueSchema.safeParse(slot.payload_schema);
        if (!payloadSchema.success) return [];
        return serializeCanonicalJsonValue(payloadSchema.data) ===
          serializeCanonicalJsonValue(HTTP_CREDENTIAL_PAYLOAD_SCHEMAS[contract])
          ? [slot.id]
          : [];
      }),
    ),
  ];
};

export type HttpCredentialSlotState = "not_required" | "declared" | "missing";

export const httpCredentialSlotState = (
  auth: unknown,
  document: WorkflowNodeConfigPanelProps["document"],
  profile: WorkflowHttpInjectionProfileAuthoringV1 | null,
): HttpCredentialSlotState => {
  if (!isRecord(auth) || auth.mode !== "endpoint_profile") {
    return "not_required";
  }
  if (profile === null || auth.injection_profile_id !== profile.id) {
    return "missing";
  }
  const selected = auth.credential_slot_id;
  return typeof selected === "string" &&
    httpCredentialSlotIds(
      document,
      profile.credential_payload_contract,
    ).includes(selected)
    ? "declared"
    : "missing";
};

const normalizedTimeout = (value: unknown): JsonValue => {
  if (!isRecord(value)) {
    return { connect_ms: null, read_ms: null, write_ms: null };
  }
  const timeoutValue = (field: string): JsonValue =>
    value[field] === null ||
    (typeof value[field] === "number" &&
      Number.isSafeInteger(value[field]) &&
      value[field] >= 0)
      ? value[field]
      : null;
  return {
    connect_ms: timeoutValue("connect_ms"),
    read_ms: timeoutValue("read_ms"),
    write_ms: timeoutValue("write_ms"),
  };
};

const normalizedResponse = (value: unknown): JsonValue => {
  if (isRecord(value)) {
    const parsed = jsonValueSchema.safeParse(value);
    if (parsed.success) return structuredClone(parsed.data);
  }
  return {
    mode: "json",
    accepted_statuses: [{ from: 200, to: 299 }],
    schema: null,
  };
};

const normalizedCurlBody = (
  body: Readonly<{ kind: "raw"; value: string }> | null,
  contentType: string | null,
): JsonValue => {
  if (body === null) return { kind: "none" };
  const mediaType = contentType?.split(";", 1)[0]?.trim().toLowerCase();
  if (mediaType === "application/json" || mediaType?.endsWith("+json")) {
    try {
      const parsed = jsonValueSchema.safeParse(JSON.parse(body.value));
      if (parsed.success) {
        return {
          kind: "json",
          template: {
            version: 1,
            template: structuredClone(parsed.data),
            bindings: {},
          },
        };
      }
    } catch {
      // Invalid JSON stays an explicit raw-text Draft for user review.
    }
  }
  return {
    kind: "raw_text",
    content_type: contentType ?? "text/plain",
    template: {
      version: 1,
      segments: [{ kind: "text", value: body.value }],
    },
  };
};

export type HttpCurlDiffEntry = Readonly<{
  field: (typeof HTTP_CONFIG_FIELDS)[number];
  before: JsonValue;
  after: JsonValue;
}>;

export type HttpCurlPreview = Readonly<{
  config: Readonly<Record<string, JsonValue>>;
  changes: readonly HttpCurlDiffEntry[];
}>;

const httpCurlPreviewForConfig = (
  currentConfig: Record<string, JsonValue>,
  config: Record<string, JsonValue>,
): HttpCurlPreview => ({
  config,
  changes: HTTP_CONFIG_FIELDS.flatMap((field) => {
    const before = jsonValueOrNull(currentConfig[field]);
    const after = jsonValueOrNull(config[field]);
    return JSON.stringify(before) === JSON.stringify(after)
      ? []
      : [{ field, before, after }];
  }),
});

const buildHttpCurlPreview = (
  source: string,
  currentConfig: Record<string, JsonValue>,
): HttpCurlPreview => {
  const parsed = parseWorkflowHttpCurl(source);
  const url = new URL(parsed.url);
  if (
    parsed.headers.some((header) => !httpHeaderNameIsSafe(header.name)) ||
    [...url.searchParams.keys()].some((name) => !httpQueryNameIsSafe(name)) ||
    (parsed.body !== null && /\$(?:\{|[A-Za-z_])/.test(parsed.body.value))
  ) {
    throw new Error("cURL import contains a secret-like or shell-like field");
  }
  const contentType =
    parsed.headers.find((header) => header.name === "content-type")?.value ??
    null;
  const headers = parsed.headers
    .filter((header) => header.name !== "content-type")
    .map((header, index) => ({
      id: `curl_header_${index + 1}`,
      name: header.name,
      value: { kind: "literal", value: header.value },
    }));
  const query = [...url.searchParams.entries()].map(([name, value], index) => ({
    id: `curl_query_${index + 1}`,
    name,
    value: { kind: "literal", value },
  }));
  const config: Record<string, JsonValue> = {
    method: parsed.method,
    base_origin: url.origin,
    path_template: {
      version: 1,
      segments: [{ kind: "text", value: url.pathname || "/" }],
    },
    query,
    headers,
    auth: { mode: "none" },
    body: normalizedCurlBody(parsed.body, contentType),
    timeout: normalizedTimeout(currentConfig.timeout),
    response: normalizedResponse(currentConfig.response),
  };
  return httpCurlPreviewForConfig(currentConfig, config);
};

export type HttpCurlDialogError =
  | "unsafe_or_invalid"
  | "request_limit_exceeded"
  | "write_capability_required"
  | "endpoint_not_available";

export type HttpCurlDialogState = Readonly<{
  source: string;
  preview: HttpCurlPreview | null;
  error: HttpCurlDialogError | null;
}>;

export const createHttpCurlDialogState = (
  source = "",
): HttpCurlDialogState => ({ source, preview: null, error: null });

export const previewHttpCurlDialog = (
  state: HttpCurlDialogState,
  currentConfig: Record<string, JsonValue>,
  canWrite: boolean,
  maxRequestBytes: number | null,
  httpAuthoring: WorkflowHttpAuthoringV1 | null,
): HttpCurlDialogState => {
  try {
    let preview = buildHttpCurlPreview(state.source, currentConfig);
    if (httpMethodIsWrite(preview.config.method) && !canWrite) {
      return { ...state, preview: null, error: "write_capability_required" };
    }
    const method = safeHttpMethod(preview.config.method);
    if (typeof preview.config.base_origin !== "string") {
      return { ...state, preview: null, error: "endpoint_not_available" };
    }
    const previewOrigin = preview.config.base_origin;
    const effectivePreviewOrigin = new URL(previewOrigin).origin;
    const endpointMatches =
      httpAuthoring?.endpoints.filter(
        (candidate) =>
          new URL(candidate.origin).origin === effectivePreviewOrigin &&
          candidate.allowed_methods.includes(method),
      ) ?? [];
    if (endpointMatches.length !== 1) {
      return { ...state, preview: null, error: "endpoint_not_available" };
    }
    const endpoint = endpointMatches[0]!;
    if (endpoint.origin !== previewOrigin) {
      preview = httpCurlPreviewForConfig(currentConfig, {
        ...preview.config,
        base_origin: endpoint.origin,
      });
    }
    if (
      maxRequestBytes !== null &&
      new TextEncoder().encode(
        JSON.stringify({
          path_template: preview.config.path_template,
          query: preview.config.query,
          headers: preview.config.headers,
          body: preview.config.body,
        }),
      ).byteLength > maxRequestBytes
    ) {
      return { ...state, preview: null, error: "request_limit_exceeded" };
    }
    return { ...state, preview, error: null };
  } catch {
    return { ...state, preview: null, error: "unsafe_or_invalid" };
  }
};

export const closeHttpCurlDialog = (): HttpCurlDialogState =>
  createHttpCurlDialogState();

export const applyHttpCurlDialog = (
  state: HttpCurlDialogState,
  currentConfig: Record<string, JsonValue>,
  canWrite: boolean,
  maxRequestBytes: number | null,
  httpAuthoring: WorkflowHttpAuthoringV1 | null,
): {
  state: HttpCurlDialogState;
  config: Record<string, JsonValue> | null;
} => {
  const revalidated = previewHttpCurlDialog(
    state,
    currentConfig,
    canWrite,
    maxRequestBytes,
    httpAuthoring,
  );
  return {
    state: closeHttpCurlDialog(),
    config:
      revalidated.preview === null
        ? null
        : structuredClone(revalidated.preview.config),
  };
};

export const httpRequestConfigIssues = (
  config: Record<string, unknown>,
  canWrite: boolean,
  maxTimeoutMs: number | null,
): string[] => {
  const issues: string[] = [];
  if (!httpBaseOriginIsSafe(config.base_origin)) {
    issues.push(
      "Base origin 必须是 canonical HTTPS origin，且不能包含路径、查询或凭据。",
    );
  }
  if (!httpPathTemplateIsSafe(config.path_template)) {
    issues.push("Path 模板必须以 literal / 开始。");
  }
  if (httpMethodIsWrite(config.method) && !canWrite) {
    issues.push("缺少 workflow.http.write，写方法不可保存、发布或运行。");
  }
  const headers = Array.isArray(config.headers) ? config.headers : [];
  headers.forEach((header, index) => {
    if (!isRecord(header) || !httpHeaderNameIsSafe(header.name)) {
      issues.push(`Header ${index + 1} 名称受控或不安全。`);
    }
  });
  const query = Array.isArray(config.query) ? config.query : [];
  query.forEach((item, index) => {
    if (!isRecord(item) || !httpQueryNameIsSafe(item.name)) {
      issues.push(`Query ${index + 1} 名称为空、像秘密或不安全。`);
    }
  });
  if (
    (config.method === "GET" || config.method === "HEAD") &&
    isRecord(config.body) &&
    config.body.kind !== "none"
  ) {
    issues.push("GET/HEAD 的 Body 必须固定为 none。");
  }
  const timeout = isRecord(config.timeout) ? config.timeout : {};
  for (const field of ["connect_ms", "read_ms", "write_ms"] as const) {
    const value = timeout[field];
    if (
      value !== null &&
      value !== undefined &&
      (typeof value !== "number" ||
        !Number.isSafeInteger(value) ||
        value <= 0 ||
        maxTimeoutMs === null ||
        value > maxTimeoutMs)
    ) {
      issues.push(`${field} 必须为 null 或不超过 public limit 的正整数。`);
    }
  }
  if (!httpRequestNodeConfigV1Schema.safeParse(config).success) {
    issues.push("HTTP Request Draft 尚未满足发布合同，可继续补齐缺失字段。");
  }
  if (
    Object.keys(config).some(
      (field) =>
        !HTTP_CONFIG_FIELDS.includes(
          field as (typeof HTTP_CONFIG_FIELDS)[number],
        ),
    )
  ) {
    issues.push("HTTP 配置包含不支持的字段；这些字段不会显示或写回。");
  }
  return [...new Set(issues)];
};
