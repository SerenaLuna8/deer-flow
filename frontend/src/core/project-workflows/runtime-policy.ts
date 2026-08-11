import { z } from "zod";

import { workflowNodeKindSchema } from "./types";

export const WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE =
  "new_workflow_runs" as const;

export const WORKFLOW_CODE_PROVIDER_ADAPTER_KEYS = [
  "aio_isolated_code_v1",
  "provisioner_isolated_code_v1",
] as const;

export const WORKFLOW_RUNTIME_MAX_INPUT_BYTES = 2_097_152;
export const WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES = 65_536;
export const WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS = 254;

const MAX_BOUNDED_BYTES = 2_147_483_648;
const MAX_BOUNDED_MILLISECONDS = 31_536_000_000;
const MAX_HTTP_RESPONSE_BYTES = 2_097_152;
const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;

const safeIntegerSchema = (minimum: number, maximum: number) =>
  z
    .number()
    .int()
    .min(minimum)
    .max(maximum)
    .refine(Number.isSafeInteger, "integer exceeds JavaScript safe range");

const positiveIntegerSchema = safeIntegerSchema(1, MAX_SAFE_INTEGER);
const nonnegativeIntegerSchema = safeIntegerSchema(0, MAX_SAFE_INTEGER);
const boundedBytesSchema = safeIntegerSchema(1, MAX_BOUNDED_BYTES);
const boundedMillisecondsSchema = safeIntegerSchema(
  1,
  MAX_BOUNDED_MILLISECONDS,
);
const stableKeySchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z0-9][a-z0-9._-]*$/);
const sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/);
const imageDigestSchema = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const headerNameSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z0-9!#$%&'*+.^_`|~-]+$/);
const uuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);

const addIssue = (
  context: z.RefinementCtx,
  message: string,
  path: Array<string | number> = [],
) => {
  context.addIssue({ code: "custom", message, path });
};

const hasDuplicates = (values: readonly string[]) =>
  new Set(values).size !== values.length;

const isCanonicalSubsetOrder = <Value extends string>(
  values: readonly Value[],
  canonicalValues: readonly Value[],
) => {
  const indexes = new Map(
    canonicalValues.map((value, index) => [value, index] as const),
  );
  return values.every(
    (value, index) =>
      index === 0 || indexes.get(values[index - 1]!)! < indexes.get(value)!,
  );
};

export const workflowAllowedNodeTypeVersionsV1Schema = z
  .object({
    type: workflowNodeKindSchema,
    versions: z.tuple([z.literal(1)]),
  })
  .strict();

export const workflowCatalogPolicyV1Schema = z
  .object({
    allowed_type_versions: z
      .array(workflowAllowedNodeTypeVersionsV1Schema)
      .max(9),
  })
  .strict()
  .superRefine((value, context) => {
    const nodeTypes = value.allowed_type_versions.map((item) => item.type);
    if (hasDuplicates(nodeTypes)) {
      addIssue(context, "Workflow catalog entries must be unique", [
        "allowed_type_versions",
      ]);
    }
    if (!isCanonicalSubsetOrder(nodeTypes, workflowNodeKindSchema.options)) {
      addIssue(context, "Workflow catalog entries must use canonical order", [
        "allowed_type_versions",
      ]);
    }
  });

export const workflowGraphLimitsV1Schema = z
  .object({
    max_nodes: safeIntegerSchema(2, 10_000),
    max_edges: safeIntegerSchema(1, 50_000),
    max_depth: safeIntegerSchema(1, 1_000),
    max_total_steps: safeIntegerSchema(2, 10_000_000),
    max_recursion_depth: safeIntegerSchema(1, 10_000_000),
    max_parallelism: safeIntegerSchema(1, 1_024),
    max_fan_out: safeIntegerSchema(1, 10_000),
    max_loops: safeIntegerSchema(0, 1_000),
    max_loop_body_nodes: safeIntegerSchema(1, 10_000),
    max_loop_body_edges: safeIntegerSchema(1, 50_000),
    max_loop_iterations: safeIntegerSchema(1, 1_000_000),
    max_total_iterations: safeIntegerSchema(1, 10_000_000),
    max_total_activations: safeIntegerSchema(2, 10_000_000),
    max_aggregate_groups: safeIntegerSchema(
      1,
      WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS,
    ),
    max_aggregate_candidates: safeIntegerSchema(1, 100_000),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.max_loop_body_nodes > value.max_nodes) {
      addIssue(context, "Loop body node limit exceeds Workflow node limit");
    }
    if (value.max_loop_body_edges > value.max_edges) {
      addIssue(context, "Loop body edge limit exceeds Workflow edge limit");
    }
    if (value.max_loop_iterations > value.max_total_iterations) {
      addIssue(context, "Loop iteration limit exceeds Run iteration limit");
    }
    if (value.max_total_steps > value.max_total_activations) {
      addIssue(context, "activation limit is below total step limit");
    }
  });

export const workflowExecutionLimitsV1Schema = z
  .object({
    max_node_timeout_ms: boundedMillisecondsSchema,
    max_run_timeout_ms: boundedMillisecondsSchema,
    max_human_wait_timeout_ms: boundedMillisecondsSchema,
    max_input_bytes: safeIntegerSchema(1, WORKFLOW_RUNTIME_MAX_INPUT_BYTES),
    max_state_bytes: boundedBytesSchema,
    max_output_bytes: boundedBytesSchema,
    max_event_preview_bytes: safeIntegerSchema(
      1,
      WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES,
    ),
    max_retry_attempts: safeIntegerSchema(0, 100),
    retry_backoff_initial_ms: boundedMillisecondsSchema,
    retry_backoff_max_ms: boundedMillisecondsSchema,
    max_llm_tokens_per_call: safeIntegerSchema(1, 10_000_000),
    max_llm_calls: safeIntegerSchema(0, 1_000_000),
    max_code_activations: safeIntegerSchema(0, 1_000_000),
    max_code_duration_ms: boundedMillisecondsSchema,
    max_http_calls: safeIntegerSchema(0, 1_000_000),
    max_http_request_bytes: boundedBytesSchema,
    max_http_response_bytes: boundedBytesSchema,
    max_http_total_bytes: boundedBytesSchema,
    max_mcp_calls: safeIntegerSchema(0, 1_000_000),
    max_files: safeIntegerSchema(0, 1_000_000),
    max_file_bytes: safeIntegerSchema(0, MAX_BOUNDED_BYTES),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.max_node_timeout_ms > value.max_run_timeout_ms) {
      addIssue(context, "node timeout exceeds Workflow Run timeout");
    }
    if (value.retry_backoff_initial_ms > value.retry_backoff_max_ms) {
      addIssue(context, "initial retry backoff exceeds maximum retry backoff");
    }
    if (
      value.max_http_total_bytes <
      Math.max(value.max_http_request_bytes, value.max_http_response_bytes)
    ) {
      addIssue(context, "HTTP cumulative limit is below a single transfer");
    }
  });

export const workflowCodeHardLimitsV1Schema = z
  .object({
    cpu_millicores: safeIntegerSchema(1, 64_000),
    memory_bytes: boundedBytesSchema,
    max_pids: safeIntegerSchema(1, 4_096),
    tmpfs_bytes: boundedBytesSchema,
    wall_timeout_ms: boundedMillisecondsSchema,
    max_source_bytes: boundedBytesSchema,
    max_stdout_bytes: boundedBytesSchema,
    max_stderr_bytes: boundedBytesSchema,
    max_result_bytes: boundedBytesSchema,
    max_total_log_bytes: boundedBytesSchema,
    read_only_root_filesystem: z.literal(true),
    allow_mounts: z.literal(false),
    allow_host_environment: z.literal(false),
    allow_credentials: z.literal(false),
    allow_runtime_sockets: z.literal(false),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.max_total_log_bytes <
      Math.max(value.max_stdout_bytes, value.max_stderr_bytes)
    ) {
      addIssue(context, "Code total log limit is below a stream limit");
    }
  });

export const workflowCodePolicyV1Schema = z
  .object({
    enabled: z.boolean(),
    provider_adapter_key: z
      .enum(WORKFLOW_CODE_PROVIDER_ADAPTER_KEYS)
      .nullable(),
    execution_profile_id: stableKeySchema.nullable(),
    runtime_contract: z.literal("python3.12-v1"),
    image_digest: imageDigestSchema.nullable(),
    isolation_profile: stableKeySchema.nullable(),
    network_policy: z.literal("deny_all"),
    dns_policy: z.literal("deny_all"),
    hard_limits: workflowCodeHardLimitsV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    const profileValues = [
      value.provider_adapter_key,
      value.execution_profile_id,
      value.image_digest,
      value.isolation_profile,
    ];
    const populated = profileValues.filter((item) => item !== null).length;
    if (populated !== 0 && populated !== profileValues.length) {
      addIssue(context, "Code execution profile must be selected atomically");
    }
    if (value.enabled && populated !== profileValues.length) {
      addIssue(context, "enabled Code requires an exact static profile");
    }
  });

const transportControlledHeaderNames = new Set([
  "authentication-info",
  "connection",
  "content-length",
  "cookie",
  "forwarded",
  "host",
  "keep-alive",
  "set-cookie",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "www-authenticate",
]);

const isTransportControlledHeader = (value: string) =>
  transportControlledHeaderNames.has(value) ||
  value.startsWith("proxy-") ||
  value.startsWith("x-forwarded-");

export const workflowHttpInjectionProfileV1Schema = z
  .object({
    id: stableKeySchema,
    location: z.literal("header"),
    scheme: z.enum(["bearer", "basic", "api_key"]),
    target_header: headerNameSchema,
    credential_payload_contract: z.enum([
      "bearer_token_v1",
      "basic_auth_v1",
      "api_key_v1",
    ]),
  })
  .strict()
  .superRefine((value, context) => {
    const expectedContract = {
      bearer: "bearer_token_v1",
      basic: "basic_auth_v1",
      api_key: "api_key_v1",
    }[value.scheme];
    if (value.credential_payload_contract !== expectedContract) {
      addIssue(context, "HTTP injection scheme and payload contract differ");
    }
    if (
      (value.scheme === "bearer" || value.scheme === "basic") &&
      value.target_header !== "authorization"
    ) {
      addIssue(context, "Bearer and Basic must target authorization");
    }
    if (value.scheme === "api_key" && value.target_header === "authorization") {
      addIssue(context, "API key profiles must target a custom safe header");
    }
    if (isTransportControlledHeader(value.target_header)) {
      addIssue(context, "injection targets a transport-controlled header");
    }
  });

const nonCanonicalNumericHost =
  /^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$/i;
const canonicalDnsLabel = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

const fixedHttpsOriginSchema = z
  .string()
  .min(1)
  .max(2_048)
  .superRefine((value, context) => {
    if (
      !/^[\x00-\x7f]+$/.test(value) ||
      value.includes("\\") ||
      value.includes("%")
    ) {
      addIssue(context, "origin must use canonical ASCII syntax");
      return;
    }
    const authority = /^https:\/\/([^/?#]+)$/.exec(value)?.[1];
    if (
      !authority ||
      authority.includes("@") ||
      authority !== authority.toLowerCase()
    ) {
      addIssue(context, "origin must be one lowercase HTTPS authority");
      return;
    }
    if (authority.startsWith("[")) {
      addIssue(context, "origin cannot use an IP literal");
      return;
    }
    const portMatch = /:([0-9]+)$/.exec(authority);
    const port = portMatch === null ? null : Number(portMatch[1]);
    const rawHostname = portMatch
      ? authority.slice(0, -(portMatch[0]?.length ?? 0))
      : authority;
    if (
      rawHostname.includes(":") ||
      (portMatch !== null && String(Number(portMatch[1])) !== portMatch[1]) ||
      (port !== null && (port < 1 || port > 65_535)) ||
      nonCanonicalNumericHost.test(rawHostname)
    ) {
      addIssue(context, "origin cannot use an IP or numeric host");
      return;
    }
    let parsed: URL;
    try {
      parsed = new URL(value);
    } catch {
      addIssue(context, "origin is invalid");
      return;
    }
    const hostname = parsed.hostname.toLowerCase();
    const labels = hostname.split(".");
    if (
      hostname.endsWith(".") ||
      hostname === "localhost" ||
      hostname === "localhost.localdomain" ||
      hostname.endsWith(".localhost") ||
      hostname.endsWith(".local") ||
      hostname.endsWith(".internal") ||
      hostname.length > 253 ||
      labels.length < 2 ||
      labels.some((label) => !canonicalDnsLabel.test(label))
    ) {
      addIssue(context, "origin must use a canonical public DNS hostname");
    }
  });

const httpMethods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] as const;

export const workflowHttpEndpointPolicyV1Schema = z
  .object({
    id: stableKeySchema,
    origin: fixedHttpsOriginSchema,
    allowed_methods: z.array(z.enum(httpMethods)).min(1).max(6),
    injection_profile_ids: z.array(stableKeySchema).max(32),
    write_idempotency: z.enum(["none", "server_derived_key"]),
    idempotency_header: headerNameSchema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      hasDuplicates(value.allowed_methods) ||
      !isCanonicalSubsetOrder(value.allowed_methods, httpMethods)
    ) {
      addIssue(context, "HTTP methods must be unique and canonical");
    }
    if (
      hasDuplicates(value.injection_profile_ids) ||
      !isCanonicalSubsetOrder(
        value.injection_profile_ids,
        [...value.injection_profile_ids].sort(),
      )
    ) {
      addIssue(context, "HTTP injection references must be unique and sorted");
    }
    const hasWriteMethod = value.allowed_methods.some((method) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(method),
    );
    if (
      value.write_idempotency === "server_derived_key" &&
      (!hasWriteMethod || value.idempotency_header === null)
    ) {
      addIssue(context, "server-derived idempotency requires write and header");
    }
    if (
      value.write_idempotency === "none" &&
      value.idempotency_header !== null
    ) {
      addIssue(context, "idempotency header requires server-derived policy");
    }
    if (
      value.idempotency_header !== null &&
      (value.idempotency_header === "authorization" ||
        isTransportControlledHeader(value.idempotency_header))
    ) {
      addIssue(context, "idempotency header is transport controlled");
    }
  });

export const workflowHttpTransportPolicyV1Schema = z
  .object({
    connect_timeout_ms: boundedMillisecondsSchema,
    read_timeout_ms: boundedMillisecondsSchema,
    write_timeout_ms: boundedMillisecondsSchema,
    total_timeout_ms: boundedMillisecondsSchema,
    max_headers: safeIntegerSchema(1, 64),
    max_header_name_bytes: safeIntegerSchema(1, 128),
    max_header_value_bytes: safeIntegerSchema(1, 4_096),
    max_request_bytes: boundedBytesSchema,
    max_wire_response_bytes: safeIntegerSchema(1, MAX_HTTP_RESPONSE_BYTES),
    max_decompressed_response_bytes: safeIntegerSchema(
      1,
      MAX_HTTP_RESPONSE_BYTES,
    ),
    max_json_depth: safeIntegerSchema(1, 64),
    max_retries: safeIntegerSchema(0, 100),
    retry_backoff_initial_ms: boundedMillisecondsSchema,
    retry_backoff_max_ms: boundedMillisecondsSchema,
    max_retry_after_ms: boundedMillisecondsSchema,
    tls_verify: z.literal(true),
    follow_redirects: z.literal(false),
    cookie_jar: z.literal(false),
    trust_env: z.literal(false),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.total_timeout_ms <
      Math.max(
        value.connect_timeout_ms,
        value.read_timeout_ms,
        value.write_timeout_ms,
      )
    ) {
      addIssue(context, "HTTP total timeout is below a phase timeout");
    }
    if (value.retry_backoff_initial_ms > value.retry_backoff_max_ms) {
      addIssue(context, "initial HTTP retry backoff exceeds maximum");
    }
    if (value.max_wire_response_bytes > value.max_decompressed_response_bytes) {
      addIssue(context, "decompressed limit is below wire response limit");
    }
  });

export const workflowHttpPolicyV1Schema = z
  .object({
    enabled: z.boolean(),
    write_enabled: z.boolean(),
    egress_profile_id: stableKeySchema.nullable(),
    egress_profile_digest: sha256HexSchema.nullable(),
    endpoint_policies: z.array(workflowHttpEndpointPolicyV1Schema).max(64),
    injection_profiles: z.array(workflowHttpInjectionProfileV1Schema).max(64),
    transport: workflowHttpTransportPolicyV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.write_enabled && !value.enabled) {
      addIssue(context, "HTTP write cannot be enabled while HTTP is disabled");
    }
    if (
      (value.egress_profile_id === null) !==
      (value.egress_profile_digest === null)
    ) {
      addIssue(context, "HTTP egress profile identity must be atomic");
    }
    if (
      value.enabled &&
      (value.egress_profile_id === null || value.endpoint_policies.length === 0)
    ) {
      addIssue(context, "enabled HTTP requires egress and endpoint policy");
    }
    const injectionIds = value.injection_profiles.map((item) => item.id);
    const endpointIds = value.endpoint_policies.map((item) => item.id);
    if (
      hasDuplicates(injectionIds) ||
      injectionIds.join("\0") !== [...injectionIds].sort().join("\0")
    ) {
      addIssue(context, "HTTP injection profiles must be unique and sorted");
    }
    if (
      hasDuplicates(endpointIds) ||
      endpointIds.join("\0") !== [...endpointIds].sort().join("\0")
    ) {
      addIssue(context, "HTTP endpoint policies must be unique and sorted");
    }
    const injectionById = new Map(
      value.injection_profiles.map((profile) => [profile.id, profile] as const),
    );
    for (const endpoint of value.endpoint_policies) {
      if (
        endpoint.injection_profile_ids.some(
          (profileId) => !injectionById.has(profileId),
        )
      ) {
        addIssue(context, "HTTP endpoint references unknown injection profile");
      }
      if (
        endpoint.idempotency_header !== null &&
        endpoint.injection_profile_ids.some(
          (profileId) =>
            injectionById.get(profileId)?.target_header ===
            endpoint.idempotency_header,
        )
      ) {
        addIssue(
          context,
          "Credential injection collides with idempotency header",
        );
      }
    }
  });

export const workflowRetentionPolicyV1Schema = z
  .object({
    terminal_run_days: safeIntegerSchema(1, 3_650),
    event_days: safeIntegerSchema(1, 3_650),
    http_effect_days: safeIntegerSchema(1, 3_650),
    destroyed_code_lease_days: safeIntegerSchema(1, 3_650),
  })
  .strict();

export const workflowFutureCapabilitiesV1Schema = z
  .object({
    human_input_enabled: z.literal(false),
    agent_enabled: z.literal(false),
    tool_enabled: z.literal(false),
    mcp_enabled: z.literal(false),
    iteration_enabled: z.literal(false),
    subworkflow_enabled: z.literal(false),
    automation_enabled: z.literal(false),
    chatflow_enabled: z.literal(false),
  })
  .strict();

export const workflowRuntimePolicyV1Schema = z
  .object({
    schema_version: z.literal(1),
    enabled: z.boolean(),
    admission_enabled: z.boolean(),
    catalog: workflowCatalogPolicyV1Schema,
    graph_limits: workflowGraphLimitsV1Schema,
    execution_limits: workflowExecutionLimitsV1Schema,
    code: workflowCodePolicyV1Schema,
    http: workflowHttpPolicyV1Schema,
    retention: workflowRetentionPolicyV1Schema,
    future: workflowFutureCapabilitiesV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.admission_enabled && !value.enabled) {
      addIssue(context, "Workflow admission cannot widen disabled Workflow");
    }
    if (
      value.code.enabled &&
      value.execution_limits.max_code_activations === 0
    ) {
      addIssue(context, "enabled Code requires an activation budget");
    }
    if (value.http.enabled && value.execution_limits.max_http_calls === 0) {
      addIssue(context, "enabled HTTP requires a call budget");
    }
    if (
      value.code.hard_limits.wall_timeout_ms >
      value.execution_limits.max_node_timeout_ms
    ) {
      addIssue(context, "Code wall timeout exceeds node timeout");
    }
    if (
      value.code.enabled &&
      value.code.hard_limits.wall_timeout_ms >
        value.execution_limits.max_code_duration_ms
    ) {
      addIssue(context, "Code wall timeout exceeds Run Code budget");
    }
    if (
      value.code.hard_limits.max_result_bytes >
      value.execution_limits.max_output_bytes
    ) {
      addIssue(context, "Code result limit exceeds Workflow output limit");
    }
    if (
      value.execution_limits.max_http_request_bytes >
      value.http.transport.max_request_bytes
    ) {
      addIssue(context, "HTTP request budget exceeds transport hard limit");
    }
    if (
      value.execution_limits.max_http_response_bytes >
      value.http.transport.max_decompressed_response_bytes
    ) {
      addIssue(context, "HTTP response budget exceeds transport hard limit");
    }
  });

export type WorkflowRuntimePolicyV1 = z.infer<
  typeof workflowRuntimePolicyV1Schema
>;

const serializeCanonicalJson = (value: unknown): string => {
  if (value === null || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError(
        "Workflow runtime policy checksum accepts safe integers only",
      );
    }
    return String(value);
  }
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(serializeCanonicalJson).join(",")}]`;
  }
  if (typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
      .map(
        ([key, item]) =>
          `${JSON.stringify(key)}:${serializeCanonicalJson(item)}`,
      )
      .join(",")}}`;
  }
  throw new TypeError("Workflow runtime policy checksum input is not JSON");
};

export const serializeWorkflowRuntimePolicyChecksumInput = (
  value: WorkflowRuntimePolicyV1,
) => serializeCanonicalJson(workflowRuntimePolicyV1Schema.parse(value));

const SHA256_INITIAL = [
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
  0x1f83d9ab, 0x5be0cd19,
] as const;
const SHA256_ROUND = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
] as const;

const rotateRight = (value: number, count: number) =>
  (value >>> count) | (value << (32 - count));

const sha256 = (input: string) => {
  const bytes = new TextEncoder().encode(input);
  const bitLength = bytes.length * 8;
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 2 ** 32));
  view.setUint32(paddedLength - 4, bitLength >>> 0);

  const hash: number[] = [...SHA256_INITIAL];
  const words = new Uint32Array(64);
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + index * 4);
    }
    for (let index = 16; index < 64; index += 1) {
      const word15 = words[index - 15]!;
      const word2 = words[index - 2]!;
      const sigma0 =
        rotateRight(word15, 7) ^ rotateRight(word15, 18) ^ (word15 >>> 3);
      const sigma1 =
        rotateRight(word2, 17) ^ rotateRight(word2, 19) ^ (word2 >>> 10);
      words[index] =
        (words[index - 16]! + sigma0 + words[index - 7]! + sigma1) >>> 0;
    }

    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index += 1) {
      const sum1 =
        rotateRight(e!, 6) ^ rotateRight(e!, 11) ^ rotateRight(e!, 25);
      const choice = (e! & f!) ^ (~e! & g!);
      const temporary1 =
        (h! + sum1 + choice + SHA256_ROUND[index]! + words[index]!) >>> 0;
      const sum0 =
        rotateRight(a!, 2) ^ rotateRight(a!, 13) ^ rotateRight(a!, 22);
      const majority = (a! & b!) ^ (a! & c!) ^ (b! & c!);
      const temporary2 = (sum0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d! + temporary1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temporary1 + temporary2) >>> 0;
    }
    hash[0] = (hash[0]! + a!) >>> 0;
    hash[1] = (hash[1]! + b!) >>> 0;
    hash[2] = (hash[2]! + c!) >>> 0;
    hash[3] = (hash[3]! + d!) >>> 0;
    hash[4] = (hash[4]! + e!) >>> 0;
    hash[5] = (hash[5]! + f!) >>> 0;
    hash[6] = (hash[6]! + g!) >>> 0;
    hash[7] = (hash[7]! + h!) >>> 0;
  }
  return hash.map((word) => word.toString(16).padStart(8, "0")).join("");
};

export const workflowRuntimePolicyChecksum = (value: WorkflowRuntimePolicyV1) =>
  sha256(serializeWorkflowRuntimePolicyChecksumInput(value));

export const workflowRuntimePolicyUpdateRequestV1Schema = z
  .object({
    expected_revision: nonnegativeIntegerSchema,
    value: workflowRuntimePolicyV1Schema,
  })
  .strict();

export const workflowRuntimeStoredPolicyV1Schema = z
  .object({
    policy_version_id: uuidSchema,
    revision: positiveIntegerSchema,
    schema_version: z.literal(1),
    payload_checksum: sha256HexSchema,
    value: workflowRuntimePolicyV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.schema_version !== value.value.schema_version) {
      addIssue(context, "stored schema version differs from policy value");
    }
    if (value.payload_checksum !== workflowRuntimePolicyChecksum(value.value)) {
      addIssue(context, "stored checksum differs from policy value");
    }
  });

export const workflowRuntimeEffectivePolicyV1Schema = z
  .object({
    policy_version_id: uuidSchema,
    revision: positiveIntegerSchema,
    schema_version: z.literal(1),
    payload_checksum: sha256HexSchema,
  })
  .strict();

export const workflowRuntimeReadinessV1Schema = z.discriminatedUnion("code", [
  z
    .object({
      status: z.literal("ready"),
      code: z.literal("WORKFLOW_RUNTIME_READY"),
      admission_ready: z.boolean(),
    })
    .strict(),
  z
    .object({
      status: z.literal("ready"),
      code: z.literal("WORKFLOW_RUNTIME_DISABLED"),
      admission_ready: z.literal(false),
    })
    .strict(),
  z
    .object({
      status: z.literal("pending"),
      code: z.literal("WORKFLOW_RUNTIME_PENDING"),
      admission_ready: z.literal(false),
    })
    .strict(),
  z
    .object({
      status: z.literal("unavailable"),
      code: z.literal("WORKFLOW_RUNTIME_UNAVAILABLE"),
      admission_ready: z.literal(false),
    })
    .strict(),
]);

const pendingRoleSchema = z.enum(["gateway", "worker", "scheduler"]);
const adminPolicyShape = {
  section: z.literal("workflow_runtime"),
  stored: workflowRuntimeStoredPolicyV1Schema,
  effective: workflowRuntimeEffectivePolicyV1Schema.nullable(),
  effect_scope: z.literal(WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE),
  pending_roles: z.array(pendingRoleSchema).max(3),
  readiness: workflowRuntimeReadinessV1Schema,
};
const adminPolicyObjectSchema = z.object(adminPolicyShape).strict();
type WorkflowRuntimeAdminPolicyV1 = z.infer<typeof adminPolicyObjectSchema>;

const identity = (
  value: z.infer<typeof workflowRuntimeEffectivePolicyV1Schema> | null,
) =>
  value === null
    ? null
    : [
        value.policy_version_id,
        value.revision,
        value.schema_version,
        value.payload_checksum,
      ].join(":");

const refineAdminPolicy = (
  value: WorkflowRuntimeAdminPolicyV1,
  context: z.RefinementCtx,
) => {
  const roles = value.pending_roles;
  const canonicalRoles = ["gateway", "worker", "scheduler"] as const;
  if (hasDuplicates(roles) || !isCanonicalSubsetOrder(roles, canonicalRoles)) {
    addIssue(context, "pending roles must be unique and canonical", [
      "pending_roles",
    ]);
  }
  const storedIdentity = identity(value.stored);
  const effectiveIdentity = identity(value.effective);
  if (value.readiness.status === "ready") {
    if (roles.length !== 0) {
      addIssue(context, "ready projection cannot have pending roles");
    }
    if (storedIdentity !== effectiveIdentity) {
      addIssue(context, "ready projection requires exact effective identity");
    }
    if (value.readiness.code === "WORKFLOW_RUNTIME_DISABLED") {
      if (value.stored.value.enabled) {
        addIssue(context, "disabled readiness requires disabled policy");
      }
    } else {
      if (!value.stored.value.enabled) {
        addIssue(context, "ready readiness requires enabled policy");
      }
      if (
        value.readiness.admission_ready !== value.stored.value.admission_enabled
      ) {
        addIssue(context, "admission readiness differs from effective policy");
      }
    }
  } else if (value.readiness.status === "pending") {
    if (!value.stored.value.enabled || !value.stored.value.admission_enabled) {
      addIssue(context, "pending projection requires admission-enabled policy");
    }
    if (roles.length !== 1 || roles[0] !== "worker") {
      addIssue(context, "pending projection requires only the worker role", [
        "pending_roles",
      ]);
    }
    if (storedIdentity !== effectiveIdentity) {
      addIssue(
        context,
        "pending projection requires Gateway-effective exact stored identity",
      );
    }
  } else if (
    value.effective !== null ||
    roles.length !== 1 ||
    roles[0] !== "gateway"
  ) {
    addIssue(
      context,
      "unavailable projection requires null effective identity and only gateway pending",
    );
  }
};

export const workflowRuntimeAdminPolicyV1Schema =
  adminPolicyObjectSchema.superRefine(refineAdminPolicy);

export const workflowRuntimePolicyUpdateResponseV1Schema = z
  .object({
    ...adminPolicyShape,
    catalog_revision: positiveIntegerSchema,
  })
  .strict()
  .superRefine(refineAdminPolicy);
