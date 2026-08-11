import { z } from "zod";

import { serializeCanonicalJsonValue } from "@/core/project-workflows/canonical";
import { valueTypeFromJsonSchema } from "@/core/project-workflows/json-schema";
import nodeRegistryV1Manifest from "@/core/project-workflows/node-registry-v1.json";
import { WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS } from "@/core/project-workflows/runtime-policy";
import {
  jsonSchemaSchema,
  workflowNodeKindSchema,
  workflowSpecV1Schema,
  workflowValueTypeSchema,
  type JsonValue,
  type WorkflowNodeKind,
} from "@/core/project-workflows/types";
import { codePointBoundedString } from "@/core/project-workflows/validation";

const safeIdentifierSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/);

const rendererKeySchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z][a-z0-9_]*$/);

const workflowCapabilitySchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^workflow\.[a-z][a-z0-9_.]*$/);

export const workflowNodeDisabledReasonCodes = [
  "WORKFLOW_DISABLED",
  "WORKFLOW_NODE_CAPABILITY_REQUIRED",
  "WORKFLOW_NODE_NOT_ALLOWED",
  "WORKFLOW_CODE_DISABLED",
  "WORKFLOW_CODE_PROFILE_UNAVAILABLE",
  "WORKFLOW_HTTP_DISABLED",
  "WORKFLOW_HTTP_PROFILE_UNAVAILABLE",
] as const;

export const workflowNodeDisabledReasonCodeSchema = z.enum(
  workflowNodeDisabledReasonCodes,
);

const sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/);

const publicIntegerLimitSchema = (maximum: number) =>
  z.number().int().min(1).max(maximum).refine(Number.isSafeInteger);

const publicBytesSchema = publicIntegerLimitSchema(2_147_483_648);
const publicHttpResponseBytesSchema = publicIntegerLimitSchema(2_097_152);
const publicMillisecondsSchema = publicIntegerLimitSchema(31_536_000_000);
const publicLoopIterationsSchema = publicIntegerLimitSchema(1_000_000);
const publicAggregateGroupsSchema = publicIntegerLimitSchema(
  WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS,
);
const publicAggregateCandidatesSchema = publicIntegerLimitSchema(100_000);

const httpAuthoringIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z0-9][a-z0-9._-]*$/);

const httpAuthoringHeaderNameSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z0-9!#$%&'*+.^_`|~-]+$/);

const httpAuthoringTransportHeaders = new Set([
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

const httpAuthoringHeaderIsTransportControlled = (value: string) =>
  httpAuthoringTransportHeaders.has(value) ||
  value.startsWith("proxy-") ||
  value.startsWith("x-forwarded-");

const httpAuthoringNonCanonicalNumericHost =
  /^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$/i;
const httpAuthoringDnsLabel = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

export const workflowHttpAuthoringOriginV1Schema = z
  .string()
  .min(1)
  .max(2_048)
  .superRefine((value, context) => {
    if (
      !/^[\x00-\x7f]+$/.test(value) ||
      value.includes("\\") ||
      value.includes("%")
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring origin must use canonical ASCII syntax",
      });
      return;
    }
    const authority = /^https:\/\/([^/?#]+)$/.exec(value)?.[1];
    if (
      !authority ||
      authority.includes("@") ||
      authority !== authority.toLowerCase() ||
      authority.startsWith("[")
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring origin must be one lowercase HTTPS authority",
      });
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
      httpAuthoringNonCanonicalNumericHost.test(rawHostname)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring origin cannot use an IP or numeric host",
      });
      return;
    }
    let parsed: URL;
    try {
      parsed = new URL(value);
    } catch {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring origin is invalid",
      });
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
      labels.some((label) => !httpAuthoringDnsLabel.test(label))
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring origin must use a canonical public DNS host",
      });
    }
  });

const httpAuthoringMethods = [
  "GET",
  "HEAD",
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
] as const;

const isCanonicalOrderedSubset = <T extends string>(
  values: readonly T[],
  order: readonly T[],
) => {
  const positions = new Map(order.map((value, index) => [value, index]));
  let previous = -1;
  for (const value of values) {
    const current = positions.get(value);
    if (current === undefined || current <= previous) return false;
    previous = current;
  }
  return true;
};

export const workflowHttpInjectionProfileAuthoringV1Schema = z
  .object({
    id: httpAuthoringIdSchema,
    scheme: z.enum(["bearer", "basic", "api_key"]),
    target_header: httpAuthoringHeaderNameSchema,
    credential_payload_contract: z.enum([
      "bearer_token_v1",
      "basic_auth_v1",
      "api_key_v1",
    ]),
  })
  .strict()
  .superRefine((profile, context) => {
    const expectedContract = {
      bearer: "bearer_token_v1",
      basic: "basic_auth_v1",
      api_key: "api_key_v1",
    }[profile.scheme];
    if (profile.credential_payload_contract !== expectedContract) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring injection scheme and contract differ",
      });
    }
    if (
      (profile.scheme === "bearer" || profile.scheme === "basic") &&
      profile.target_header !== "authorization"
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Bearer and Basic authoring profiles target authorization",
      });
    }
    if (
      profile.scheme === "api_key" &&
      profile.target_header === "authorization"
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "API key authoring profiles require a custom header",
      });
    }
    if (httpAuthoringHeaderIsTransportControlled(profile.target_header)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring profile targets a transport header",
      });
    }
  });

export const workflowHttpEndpointAuthoringV1Schema = z
  .object({
    id: httpAuthoringIdSchema,
    origin: workflowHttpAuthoringOriginV1Schema,
    allowed_methods: z.array(z.enum(httpAuthoringMethods)).min(1).max(6),
    write_idempotency: z.enum(["none", "server_derived_key"]),
    injection_profiles: z
      .array(workflowHttpInjectionProfileAuthoringV1Schema)
      .max(32),
  })
  .strict()
  .superRefine((endpoint, context) => {
    if (
      !isCanonicalOrderedSubset(endpoint.allowed_methods, httpAuthoringMethods)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring methods must be unique and canonical",
        path: ["allowed_methods"],
      });
    }
    const profileIds = endpoint.injection_profiles.map((profile) => profile.id);
    if (!isCanonicalOrderedSubset(profileIds, [...profileIds].sort())) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring profiles must be unique and sorted",
        path: ["injection_profiles"],
      });
    }
  });

export const workflowHttpAuthoringV1Schema = z
  .object({
    endpoints: z.array(workflowHttpEndpointAuthoringV1Schema).max(64),
  })
  .strict()
  .superRefine((authoring, context) => {
    const endpointIds = authoring.endpoints.map((endpoint) => endpoint.id);
    if (!isCanonicalOrderedSubset(endpointIds, [...endpointIds].sort())) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring endpoints must be unique and sorted",
        path: ["endpoints"],
      });
    }
    const coordinates = authoring.endpoints.flatMap((endpoint) => {
      let effectiveOrigin = endpoint.origin;
      try {
        effectiveOrigin = new URL(endpoint.origin).origin;
      } catch {
        // The endpoint-level origin schema reports the invalid origin.
      }
      return endpoint.allowed_methods.map(
        (method) => `${effectiveOrigin}\n${method}`,
      );
    });
    if (new Set(coordinates).size !== coordinates.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "HTTP authoring origin and method coordinates must be unique",
        path: ["endpoints"],
      });
    }
  });

export const localizedNodeTitleSchema = z
  .object({
    "zh-CN": codePointBoundedString(1, 128),
    "en-US": codePointBoundedString(1, 128),
  })
  .strict();

export const portDefinitionSchema = z
  .object({
    id: safeIdentifierSchema,
    title_i18n: localizedNodeTitleSchema,
    kind: z.enum(["control", "data"]),
    value_type: workflowValueTypeSchema.nullable(),
    cardinality: z.enum(["one", "many"]),
    required: z.boolean(),
  })
  .strict()
  .superRefine((port, context) => {
    if (port.kind === "control" && port.value_type !== null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "control ports cannot declare a data value type",
        path: ["value_type"],
      });
    }
    if (port.kind === "data" && port.value_type === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "data ports require a Workflow value type",
        path: ["value_type"],
      });
    }
  });

export const portDerivationV1Schema = z
  .object({
    version: z.literal(1),
    input_source: z.literal("none"),
    output_source: z.enum([
      "none",
      "workflow_inputs",
      "condition_branches",
      "llm_result_v1",
      "transform_result_v1",
      "aggregate_groups",
      "loop_variables",
      "http_response_body",
      "python_result_v1",
    ]),
  })
  .strict();

const retrySemanticsSchema = z.enum([
  "pure",
  "isolated_compute",
  "read",
  "idempotent_write",
  "unsafe_write",
  "http_method_v1",
  "loop_body_v1",
]);

export const nodeTypeDefinitionSchema = z
  .object({
    type: workflowNodeKindSchema,
    version: z.literal(1),
    renderer_key: rendererKeySchema,
    title_i18n: localizedNodeTitleSchema,
    config_schema: jsonSchemaSchema,
    input_ports: z.array(portDefinitionSchema).max(256),
    output_ports: z.array(portDefinitionSchema).max(256),
    port_derivation: portDerivationV1Schema,
    required_capabilities: z.array(workflowCapabilitySchema).max(32),
    retry_semantics: retrySemanticsSchema,
    supports_streaming: z.boolean(),
  })
  .strict()
  .superRefine((definition, context) => {
    if (definition.renderer_key !== definition.type) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "first-batch renderer_key must equal the stable node type",
        path: ["renderer_key"],
      });
    }

    if (definition.supports_streaming !== (definition.type === "llm")) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "only the first-batch LLM node supports streaming",
        path: ["supports_streaming"],
      });
    }

    if (
      definition.config_schema.type !== "object" ||
      definition.config_schema.additionalProperties !== false
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "node config_schema must be a closed object schema",
        path: ["config_schema"],
      });
    }

    for (const [field, ports] of [
      ["input_ports", definition.input_ports],
      ["output_ports", definition.output_ports],
    ] as const) {
      const seenPortIds = new Set<string>();
      ports.forEach((port, index) => {
        if (seenPortIds.has(port.id)) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            message: "node port ids must be unique within one direction",
            path: [field, index, "id"],
          });
        }
        seenPortIds.add(port.id);
      });
    }
  });

export const workflowNodeRegistryV1 = z
  .array(nodeTypeDefinitionSchema)
  .length(9)
  .parse(nodeRegistryV1Manifest);

export const workflowNodeCatalogKinds: readonly WorkflowNodeKind[] =
  workflowNodeRegistryV1.map((definition) => definition.type);

export const firstBatchNodeTitles = Object.fromEntries(
  workflowNodeRegistryV1.map((definition) => [
    definition.type,
    definition.title_i18n,
  ]),
) as Record<WorkflowNodeKind, LocalizedNodeTitle>;

const workflowNodeRegistryByIdentity = new Map(
  workflowNodeRegistryV1.map((definition) => [
    `${definition.type}:${definition.version}`,
    definition,
  ]),
);

export const resolvedNodeInstancePortsV1Schema = z
  .object({
    node_id: z
      .string()
      .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/),
    input_ports: z.array(portDefinitionSchema).max(256),
    output_ports: z.array(portDefinitionSchema).max(256),
  })
  .strict();

export const resolvedWorkflowInstancePortsV1Schema = z
  .object({
    schema_version: z.literal(1),
    nodes: z.array(resolvedNodeInstancePortsV1Schema),
  })
  .strict();

const dynamicTitle = (value: string) => ({
  "zh-CN": value,
  "en-US": value,
});

const derivedControlPort = (id: string, title: string): PortDefinition =>
  portDefinitionSchema.parse({
    id,
    title_i18n: dynamicTitle(title),
    kind: "control",
    value_type: null,
    cardinality: "one",
    required: true,
  });

const derivedDataPort = (
  id: string,
  title: string,
  valueType: z.infer<typeof workflowValueTypeSchema>,
): PortDefinition =>
  portDefinitionSchema.parse({
    id,
    title_i18n: dynamicTitle(title),
    kind: "data",
    value_type: valueType,
    cardinality: "many",
    required: true,
  });

const combineFixedAndDerivedPorts = (
  fixed: readonly PortDefinition[],
  derived: readonly PortDefinition[],
): PortDefinition[] => {
  const fixedIds = new Set(fixed.map((port) => port.id));
  const derivedIds = new Set<string>();
  for (const port of derived) {
    if (derivedIds.has(port.id)) {
      throw new Error(`duplicate derived port id: ${port.id}`);
    }
    if (fixedIds.has(port.id)) {
      throw new Error(
        `derived port id conflicts with a fixed port: ${port.id}`,
      );
    }
    derivedIds.add(port.id);
  }
  return [...fixed, ...derived];
};

export function resolveWorkflowInstancePortsV1(
  workflowSpec: unknown,
): ResolvedWorkflowInstancePortsV1 {
  const workflow = workflowSpecV1Schema.parse(workflowSpec);
  const resolvedNodes: ResolvedNodeInstancePortsV1[] = [];
  const resolvedByNodeId = new Map<string, ResolvedNodeInstancePortsV1>();

  for (const node of workflow.nodes) {
    if (resolvedByNodeId.has(node.id)) {
      throw new Error(`duplicate Workflow node id: ${node.id}`);
    }
    const definition = workflowNodeRegistryByIdentity.get(
      `${node.type}:${node.type_version}`,
    );
    if (definition === undefined) {
      throw new Error(`unknown Workflow node registry identity: ${node.type}`);
    }

    let derivedOutputs: PortDefinition[];
    switch (definition.port_derivation.output_source) {
      case "workflow_inputs":
        if (node.type !== "start") {
          throw new Error("workflow_inputs ports require a Start node");
        }
        derivedOutputs = workflow.workflow_inputs.map((declaration) =>
          derivedDataPort(
            declaration.id,
            declaration.label ?? declaration.name,
            declaration.value_type,
          ),
        );
        break;
      case "condition_branches":
        if (node.type !== "condition") {
          throw new Error("condition_branches ports require a Condition node");
        }
        derivedOutputs = node.config.branches.map((branch) =>
          derivedControlPort(branch.output_port_id, branch.label ?? branch.id),
        );
        derivedOutputs.push(
          derivedControlPort(node.config.else_output_port_id, "ELSE"),
        );
        break;
      case "llm_result_v1":
        if (node.type !== "llm") {
          throw new Error("llm_result_v1 ports require an LLM node");
        }
        derivedOutputs = [
          derivedDataPort(
            "result",
            "结构化结果 / Structured Result",
            node.config.structured_output.enabled
              ? valueTypeFromJsonSchema(
                  node.config.structured_output.schema!,
                  "object",
                )
              : {
                  kind: "json",
                  collection: false,
                  nullable: true,
                },
          ),
        ];
        break;
      case "transform_result_v1":
        if (node.type !== "transform") {
          throw new Error(
            "transform_result_v1 ports require a Template Transform node",
          );
        }
        derivedOutputs = [
          derivedDataPort("result", "转换结果 / Transform Result", {
            ...(node.config.mode === "text"
              ? {
                  kind: "string" as const,
                  collection: false,
                  nullable: false,
                }
              : valueTypeFromJsonSchema(node.config.output_schema)),
          }),
        ];
        break;
      case "aggregate_groups":
        if (node.type !== "variable_aggregate") {
          throw new Error(
            "aggregate_groups ports require a Variable Aggregate node",
          );
        }
        derivedOutputs = node.config.groups.map((group) =>
          derivedDataPort(group.id, group.name, group.value_type),
        );
        break;
      case "loop_variables":
        if (node.type !== "loop") {
          throw new Error("loop_variables ports require a Loop node");
        }
        derivedOutputs = node.config.variables.map((variable) =>
          derivedDataPort(
            variable.output_port_id,
            variable.name,
            variable.value_type,
          ),
        );
        break;
      case "http_response_body":
        if (node.type !== "http_request") {
          throw new Error(
            "http_response_body ports require an HTTP Request node",
          );
        }
        derivedOutputs = [
          derivedDataPort(
            "body",
            "响应体 / Response Body",
            node.config.response.mode === "text"
              ? {
                  kind: "string",
                  collection: false,
                  nullable: false,
                }
              : valueTypeFromJsonSchema(node.config.response.schema!),
          ),
        ];
        break;
      case "python_result_v1":
        if (node.type !== "python_code") {
          throw new Error("python_result_v1 ports require a Python Code node");
        }
        derivedOutputs = [
          derivedDataPort(
            "result",
            "执行结果 / Execution Result",
            valueTypeFromJsonSchema(node.config.output_schema, "object"),
          ),
        ];
        break;
      case "none":
        derivedOutputs = [];
        break;
    }
    const resolved = resolvedNodeInstancePortsV1Schema.parse({
      node_id: node.id,
      input_ports: definition.input_ports,
      output_ports: combineFixedAndDerivedPorts(
        definition.output_ports,
        derivedOutputs,
      ),
    });
    resolvedNodes.push(resolved);
    resolvedByNodeId.set(node.id, resolved);
  }

  for (const transition of workflow.transitions) {
    const source = resolvedByNodeId.get(transition.source.node_id);
    if (
      !source?.output_ports.some(
        (port) =>
          port.id === transition.source.port_id && port.kind === "control",
      )
    ) {
      throw new Error(
        `transition ${transition.id} source does not reference a resolved control port`,
      );
    }
    const target = resolvedByNodeId.get(transition.target.node_id);
    if (
      !target?.input_ports.some(
        (port) =>
          port.id === transition.target.port_id && port.kind === "control",
      )
    ) {
      throw new Error(
        `transition ${transition.id} target does not reference a resolved control port`,
      );
    }
  }

  return resolvedWorkflowInstancePortsV1Schema.parse({
    schema_version: 1,
    nodes: resolvedNodes,
  });
}

export const nodeAvailabilitySchema = z.discriminatedUnion("state", [
  z
    .object({
      state: z.literal("enabled"),
      reason_code: z.null().optional(),
    })
    .strict(),
  z
    .object({
      state: z.literal("disabled"),
      reason_code: workflowNodeDisabledReasonCodeSchema,
    })
    .strict(),
]);

export const nodePublicLimitsSchema = z
  .object({
    max_source_bytes: publicBytesSchema.nullable().optional(),
    max_timeout_ms: publicMillisecondsSchema.nullable().optional(),
    max_iterations: publicLoopIterationsSchema.nullable().optional(),
    max_aggregate_groups: publicAggregateGroupsSchema.nullable().optional(),
    max_aggregate_candidates: publicAggregateCandidatesSchema
      .nullable()
      .optional(),
    max_http_request_bytes: publicBytesSchema.nullable().optional(),
    max_http_response_bytes: publicHttpResponseBytesSchema
      .nullable()
      .optional(),
  })
  .strict();

type NodePublicLimitField = keyof z.infer<typeof nodePublicLimitsSchema>;

const publicLimitFields = [
  "max_source_bytes",
  "max_timeout_ms",
  "max_iterations",
  "max_aggregate_groups",
  "max_aggregate_candidates",
  "max_http_request_bytes",
  "max_http_response_bytes",
] as const satisfies readonly NodePublicLimitField[];

const allowedPublicLimitFields: Record<
  WorkflowNodeKind,
  readonly NodePublicLimitField[]
> = {
  start: ["max_timeout_ms"],
  llm: ["max_timeout_ms"],
  condition: ["max_timeout_ms"],
  transform: ["max_timeout_ms"],
  variable_aggregate: [
    "max_aggregate_groups",
    "max_aggregate_candidates",
    "max_timeout_ms",
  ],
  loop: ["max_iterations", "max_timeout_ms"],
  http_request: [
    "max_timeout_ms",
    "max_http_request_bytes",
    "max_http_response_bytes",
  ],
  python_code: ["max_source_bytes", "max_timeout_ms"],
  end: ["max_timeout_ms"],
};

export const nodeCatalogEntrySchema = z
  .object({
    definition: nodeTypeDefinitionSchema,
    availability: nodeAvailabilitySchema,
    public_limits: nodePublicLimitsSchema.nullable().optional(),
    http_authoring: workflowHttpAuthoringV1Schema.nullable().optional(),
  })
  .strict()
  .superRefine((entry, context) => {
    const registryDefinition = workflowNodeRegistryByIdentity.get(
      `${entry.definition.type}:${entry.definition.version}`,
    );
    if (
      registryDefinition === undefined ||
      serializeCanonicalJsonValue(entry.definition as unknown as JsonValue) !==
        serializeCanonicalJsonValue(registryDefinition as unknown as JsonValue)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "catalog entries must use the exact canonical node registry definition",
        path: ["definition"],
      });
    }

    if (
      entry.definition.type !== "http_request" &&
      entry.http_authoring !== null &&
      entry.http_authoring !== undefined
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "only the HTTP Request entry may expose HTTP authoring options",
        path: ["http_authoring"],
      });
    }

    if (entry.public_limits === null || entry.public_limits === undefined) {
      return;
    }

    const allowedFields = allowedPublicLimitFields[entry.definition.type];
    for (const field of publicLimitFields) {
      if (
        entry.public_limits[field] !== null &&
        entry.public_limits[field] !== undefined &&
        !allowedFields.includes(field)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message:
            "catalog entry exposes a public limit that does not apply to this node type",
          path: ["public_limits", field],
        });
      }
    }
  });

const canonicalNodeOrder = new Map(
  workflowNodeCatalogKinds.map((nodeType, index) => [nodeType, index]),
);

export const nodeCatalogResponseV1Schema = z
  .object({
    schema_version: z.literal(1),
    catalog_generation: sha256HexSchema,
    availability_generation: sha256HexSchema,
    entries: z.array(nodeCatalogEntrySchema).length(9),
  })
  .strict()
  .superRefine((catalog, context) => {
    const identities = new Set<string>();
    let previousOrder = -1;

    catalog.entries.forEach((entry, index) => {
      const identity = `${entry.definition.type}:${entry.definition.version}`;
      if (identities.has(identity)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message:
            "Node Catalog entries must have unique type/version identities",
          path: ["entries", index, "definition", "type"],
        });
      }
      identities.add(identity);

      const order = canonicalNodeOrder.get(entry.definition.type);
      if (order === undefined || order <= previousOrder) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Node Catalog entries must use canonical registry order",
          path: ["entries", index, "definition", "type"],
        });
      }
      previousOrder = order ?? previousOrder;
    });
  });

export type LocalizedNodeTitle = z.infer<typeof localizedNodeTitleSchema>;
export type PortDefinition = z.infer<typeof portDefinitionSchema>;
export type NodeTypeDefinition = z.infer<typeof nodeTypeDefinitionSchema>;
export type ResolvedNodeInstancePortsV1 = z.infer<
  typeof resolvedNodeInstancePortsV1Schema
>;
export type ResolvedWorkflowInstancePortsV1 = z.infer<
  typeof resolvedWorkflowInstancePortsV1Schema
>;
export type NodeAvailability = z.infer<typeof nodeAvailabilitySchema>;
export type NodePublicLimits = z.infer<typeof nodePublicLimitsSchema>;
export type WorkflowHttpInjectionProfileAuthoringV1 = z.infer<
  typeof workflowHttpInjectionProfileAuthoringV1Schema
>;
export type WorkflowHttpEndpointAuthoringV1 = z.infer<
  typeof workflowHttpEndpointAuthoringV1Schema
>;
export type WorkflowHttpAuthoringV1 = z.infer<
  typeof workflowHttpAuthoringV1Schema
>;
export type NodeCatalogEntry = z.infer<typeof nodeCatalogEntrySchema>;
export type NodeCatalogResponseV1 = z.infer<typeof nodeCatalogResponseV1Schema>;
