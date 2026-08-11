import { z } from "zod";

import { workflowValidationIssueV1Schema } from "./transport";
import {
  canvasDocumentV1Schema,
  canonicalJsonNumberSchema,
  conditionNodeConfigV1Schema,
  endNodeConfigV1Schema,
  httpRequestNodeConfigV1Schema,
  jsonValueSchema,
  llmNodeConfigV1Schema,
  loopNodeConfigV1Schema,
  pythonCodeNodeConfigV1Schema,
  startNodeConfigV1Schema,
  transformNodeConfigV1Schema,
  variableAggregateNodeConfigV1Schema,
  workflowNodeKindSchema,
  workflowSpecV1Schema,
} from "./types";
import {
  addUnicodeScalarIssues,
  codePointBoundedString,
  containsOnlyUnicodeScalars,
} from "./validation";

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const canonicalUuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const positiveSafeIntegerSchema = z.number().int().min(1).max(MAX_SAFE_INTEGER);
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const safeIdentifierSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9._:-]+$/);
const safeSlotIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z_][A-Za-z0-9_.:-]*$/);
const requestIdSchema = z
  .string()
  .min(1)
  .max(512)
  .regex(/^[\x20-\x7e]+$/);
const timestampSchema = z.string().datetime({ offset: true });
const opaqueCursorSchema = z
  .string()
  .min(1)
  .max(1_024)
  .regex(/^[!-~]+$/);
const responseCursorSchema = codePointBoundedString(1, 1_024);
const scalarStringSchema = z
  .string()
  .refine(containsOnlyUnicodeScalars, "string must contain Unicode scalars");
const scalarJsonValueSchema = jsonValueSchema.superRefine((value, context) =>
  addUnicodeScalarIssues(value, context),
);
const draftJsonObjectSchema = z
  .record(z.string(), jsonValueSchema)
  .superRefine((value, context) => addUnicodeScalarIssues(value, context));

const FORBIDDEN_DRAFT_FIELDS = new Set([
  "authority",
  "authority_id",
  "capability",
  "capabilities",
  "credential_grant_id",
  "credential_id",
  "credential_version_id",
  "envelope_id",
  "execution_profile",
  "executor",
  "executor_id",
  "membership_id",
  "membership_version",
  "owner_id",
  "owner_user_id",
  "private_scope",
  "project_context",
  "project_id",
  "project_role",
  "project_slug",
  "runtime",
  "runtime_id",
  "runtime_profile",
  "secret",
  "secret_value",
  "secrets",
  "user_id",
]);

function containsForbiddenDraftField(value: unknown): boolean {
  const stack = [value];
  while (stack.length > 0) {
    const current = stack.pop();
    if (Array.isArray(current)) {
      stack.push(...current);
      continue;
    }
    if (current === null || typeof current !== "object") continue;
    for (const [key, item] of Object.entries(current)) {
      if (key.startsWith("__") || FORBIDDEN_DRAFT_FIELDS.has(key)) {
        return true;
      }
      stack.push(item);
    }
  }
  return false;
}

const knownNodeConfigSchemas: Record<string, z.ZodTypeAny> = {
  start: startNodeConfigV1Schema,
  llm: llmNodeConfigV1Schema,
  condition: conditionNodeConfigV1Schema,
  transform: transformNodeConfigV1Schema,
  variable_aggregate: variableAggregateNodeConfigV1Schema,
  loop: loopNodeConfigV1Schema,
  http_request: httpRequestNodeConfigV1Schema,
  python_code: pythonCodeNodeConfigV1Schema,
  end: endNodeConfigV1Schema,
};

function valueAtPath(value: unknown, path: PropertyKey[]): unknown {
  let current = value;
  for (const segment of path) {
    if (current === null || typeof current !== "object") return undefined;
    current = (current as Record<PropertyKey, unknown>)[segment];
  }
  return current;
}

function isIncompleteOnlyIssue(
  issue: z.ZodIssue,
  config: Record<string, unknown>,
): boolean {
  if (issue.code === z.ZodIssueCode.invalid_union) {
    return issue.unionErrors.every((error) =>
      error.issues.every((nested) => isIncompleteOnlyIssue(nested, config)),
    );
  }
  if (issue.code === z.ZodIssueCode.invalid_union_discriminator) {
    return valueAtPath(config, issue.path) === undefined;
  }
  if (issue.code === z.ZodIssueCode.invalid_type) {
    return issue.received === "undefined";
  }
  if (issue.code === z.ZodIssueCode.invalid_literal) {
    return issue.received === undefined;
  }
  return false;
}

function partialKnownConfigIsValid(
  nodeType: string | null | undefined,
  config: Record<string, unknown>,
): boolean {
  if (!nodeType) return true;
  const schema = knownNodeConfigSchemas[nodeType];
  if (!schema) return true;
  const result = schema.safeParse(config);
  if (result.success) return true;
  return result.error.issues.every((issue) =>
    isIncompleteOnlyIssue(issue, config),
  );
}

const workflowDraftEndpointV1Schema = z
  .object({
    node_id: canonicalUuidSchema.nullable().optional(),
    port_id: safeIdentifierSchema.nullable().optional(),
  })
  .strict();

const workflowDraftTransitionV1Schema = z
  .object({
    id: safeIdentifierSchema.nullable().optional(),
    source: workflowDraftEndpointV1Schema.nullable().optional(),
    target: workflowDraftEndpointV1Schema.nullable().optional(),
  })
  .strict();

export const workflowDraftNodeV1Schema = z
  .object({
    id: canonicalUuidSchema.nullable().optional(),
    type: codePointBoundedString(1, 128).nullable().optional(),
    type_version: positiveSafeIntegerSchema.nullable().optional(),
    scope: draftJsonObjectSchema.nullable().optional(),
    custom_label: scalarStringSchema.nullable().optional(),
    description: scalarStringSchema.nullable().optional(),
    input_bindings: draftJsonObjectSchema.nullable().optional(),
    execution_policy: draftJsonObjectSchema.nullable().optional(),
    config: draftJsonObjectSchema.nullable().optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (containsForbiddenDraftField(value)) {
      context.addIssue({
        code: "custom",
        message: "Workflow Draft contains a server-owned field",
      });
    }
    if (
      value.config !== null &&
      value.config !== undefined &&
      !partialKnownConfigIsValid(value.type, value.config)
    ) {
      context.addIssue({
        code: "custom",
        message: "Workflow Draft node config contains an invalid present field",
        path: ["config"],
      });
    }
  });

const workflowDraftInputV1Schema = z
  .object({
    id: scalarStringSchema.nullable().optional(),
    name: scalarStringSchema.nullable().optional(),
    label: scalarStringSchema.nullable().optional(),
    description: scalarStringSchema.nullable().optional(),
    value_type: draftJsonObjectSchema.nullable().optional(),
    required: z.boolean().nullable().optional(),
    default: scalarJsonValueSchema.nullable().optional(),
    constraints: draftJsonObjectSchema.nullable().optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (containsForbiddenDraftField(value)) {
      context.addIssue({
        code: "custom",
        message: "Workflow Draft contains a server-owned field",
      });
    }
  });

const workflowDraftOutputV1Schema = z
  .object({
    id: scalarStringSchema.nullable().optional(),
    name: scalarStringSchema.nullable().optional(),
    description: scalarStringSchema.nullable().optional(),
    value_type: draftJsonObjectSchema.nullable().optional(),
    source: draftJsonObjectSchema.nullable().optional(),
    default: scalarJsonValueSchema.nullable().optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (containsForbiddenDraftField(value)) {
      context.addIssue({
        code: "custom",
        message: "Workflow Draft contains a server-owned field",
      });
    }
  });

const workflowDraftCredentialSlotV1Schema = z
  .object({
    id: safeSlotIdSchema.nullable().optional(),
    name: scalarStringSchema.nullable().optional(),
    purpose: z.literal("http_auth").nullable().optional(),
    payload_schema: draftJsonObjectSchema.nullable().optional(),
    required: z.literal(true).nullable().optional(),
  })
  .strict();

export const workflowDraftSpecV1Schema = z
  .object({
    schema_version: z.literal(1),
    entry_node_id: canonicalUuidSchema.nullable().optional(),
    nodes: z.array(workflowDraftNodeV1Schema).max(10_000).nullable().optional(),
    transitions: z
      .array(workflowDraftTransitionV1Schema)
      .max(50_000)
      .nullable()
      .optional(),
    workflow_inputs: z
      .array(workflowDraftInputV1Schema)
      .max(255)
      .nullable()
      .optional(),
    workflow_outputs: z
      .array(workflowDraftOutputV1Schema)
      .max(10_000)
      .nullable()
      .optional(),
    credential_slots: z
      .array(workflowDraftCredentialSlotV1Schema)
      .max(255)
      .nullable()
      .optional(),
  })
  .strict();

const workflowDraftPositionV1Schema = z
  .object({
    x: canonicalJsonNumberSchema.nullable().optional(),
    y: canonicalJsonNumberSchema.nullable().optional(),
  })
  .strict();

const workflowDraftNodeLayoutV1Schema = z
  .object({
    node_id: canonicalUuidSchema.nullable().optional(),
    position: workflowDraftPositionV1Schema.nullable().optional(),
    parent_node_id: canonicalUuidSchema.nullable().optional(),
    collapsed: z.boolean().nullable().optional(),
  })
  .strict();

const workflowDraftEdgeLayoutV1Schema = z
  .object({
    edge_id: safeIdentifierSchema.nullable().optional(),
    routing: z.enum(["bezier", "smoothstep"]).nullable().optional(),
  })
  .strict();

export const workflowDraftCanvasV1Schema = z
  .object({
    schema_version: z.literal(1),
    node_layouts: z
      .array(workflowDraftNodeLayoutV1Schema)
      .max(10_000)
      .nullable()
      .optional(),
    edge_layouts: z
      .array(workflowDraftEdgeLayoutV1Schema)
      .max(50_000)
      .nullable()
      .optional(),
  })
  .strict();

export const workflowDraftSaveRequestV1Schema = z
  .object({
    expected_revision: positiveSafeIntegerSchema,
    spec: workflowDraftSpecV1Schema,
    canvas: workflowDraftCanvasV1Schema,
  })
  .strict();

export const workflowDraftValidateRequestV1Schema = z
  .object({
    expected_revision: positiveSafeIntegerSchema,
    expected_draft_checksum: sha256Schema,
  })
  .strict();

export const workflowPublishRequestV1Schema =
  workflowDraftValidateRequestV1Schema;

const trimmedDefinitionNameSchema = codePointBoundedString(1, 255).refine(
  (value) => value === value.trim(),
  "Workflow Definition name must be trimmed",
);
const definitionDescriptionSchema = codePointBoundedString(0, 4_096);

export const workflowDefinitionCreateRequestV1Schema = z
  .object({
    name: trimmedDefinitionNameSchema,
    description: definitionDescriptionSchema,
  })
  .strict();

export const workflowDefinitionUpdateRequestV1Schema = z
  .object({
    expected_revision: positiveSafeIntegerSchema,
    name: trimmedDefinitionNameSchema.nullable().optional(),
    description: definitionDescriptionSchema.nullable().optional(),
  })
  .strict()
  .refine(
    (value) => value.name != null || value.description != null,
    "Workflow Definition update requires one field",
  );

export const workflowDefinitionArchiveRequestV1Schema = z
  .object({ expected_revision: positiveSafeIntegerSchema })
  .strict();

export const workflowDefinitionListQueryV1Schema = z
  .object({
    query: codePointBoundedString(1, 255)
      .refine((value) => value === value.trim())
      .nullable()
      .default(null),
    lifecycle: z.enum(["active", "archived"]).default("active"),
    publication: z.enum(["all", "draft_only", "published"]).default("all"),
    sort: z
      .enum(["updated_desc", "name_asc", "name_desc"])
      .default("updated_desc"),
    cursor: opaqueCursorSchema.nullable().default(null),
    limit: positiveSafeIntegerSchema.max(100).default(50),
  })
  .strict();

export const workflowVersionListQueryV1Schema = z
  .object({
    cursor: opaqueCursorSchema.nullable().default(null),
    limit: positiveSafeIntegerSchema.max(100).default(50),
  })
  .strict();

export const workflowCredentialGrantMutationRequestV1Schema = z
  .object({
    credential_id: canonicalUuidSchema,
    expected_credential_version_id: canonicalUuidSchema,
    expected_slot_schema_checksum: sha256Schema,
  })
  .strict();

export const workflowDraftResponseV1Schema = z
  .object({
    workflow_id: canonicalUuidSchema,
    revision: positiveSafeIntegerSchema,
    spec: workflowDraftSpecV1Schema,
    canvas: workflowDraftCanvasV1Schema,
    draft_checksum: sha256Schema,
    updated_at: timestampSchema,
  })
  .strict();

export const workflowDefinitionResponseV1Schema = z
  .object({
    id: canonicalUuidSchema,
    name: codePointBoundedString(1, 255),
    description: definitionDescriptionSchema,
    lifecycle: z.enum(["active", "archived"]),
    publication: z.enum(["draft_only", "published"]),
    revision: positiveSafeIntegerSchema,
    current_published_version_id: canonicalUuidSchema.nullable(),
    current_published_version_number: positiveSafeIntegerSchema.nullable(),
    draft_revision: positiveSafeIntegerSchema,
    draft_checksum: sha256Schema,
    created_at: timestampSchema,
    updated_at: timestampSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const hasPublishedId = value.current_published_version_id !== null;
    const hasPublishedNumber = value.current_published_version_number !== null;
    if (hasPublishedId !== hasPublishedNumber) {
      context.addIssue({
        code: "custom",
        message: "Workflow Definition published coordinates disagree",
      });
    }
    const expectedPublication = hasPublishedId ? "published" : "draft_only";
    if (value.publication !== expectedPublication) {
      context.addIssue({
        code: "custom",
        message: "Workflow Definition publication is not derived",
        path: ["publication"],
      });
    }
  });

export const workflowDefinitionPageV1Schema = z
  .object({
    items: z.array(workflowDefinitionResponseV1Schema).max(100),
    next_cursor: responseCursorSchema.nullable(),
  })
  .strict();

export const workflowPublishedModelRefV1Schema = z
  .object({
    node_id: canonicalUuidSchema,
    purpose: z.literal("primary"),
    logical_model_name: codePointBoundedString(1, 128),
  })
  .strict();

export const workflowPublishedCredentialSlotV1Schema = z
  .object({
    slot_id: codePointBoundedString(1, 128),
    name: codePointBoundedString(1, 255),
    purpose: z.literal("http_auth"),
    payload_schema: draftJsonObjectSchema,
    payload_schema_checksum: sha256Schema,
    required: z.literal(true),
  })
  .strict();

export const workflowPublishedCodeRequirementV1Schema = z
  .object({
    node_id: canonicalUuidSchema,
    runtime_contract: z.literal("python3.12-v1"),
  })
  .strict();

export const workflowPublishedHttpRequirementV1Schema = z
  .object({
    node_id: canonicalUuidSchema,
    method: z.enum(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]),
    endpoint_policy_id: codePointBoundedString(1, 128),
    injection_profile_id: codePointBoundedString(1, 128).nullable(),
    credential_slot_id: codePointBoundedString(1, 128).nullable(),
  })
  .strict();

export const workflowPublishedRequirementsV1Schema = z
  .object({
    node_types: z.array(workflowNodeKindSchema).min(2).max(9),
    model_refs: z.array(workflowPublishedModelRefV1Schema).max(10_000),
    code: z.array(workflowPublishedCodeRequirementV1Schema).max(10_000),
    http: z.array(workflowPublishedHttpRequirementV1Schema).max(10_000),
    credential_slots: z.array(workflowPublishedCredentialSlotV1Schema).max(255),
    requires_code: z.boolean(),
    requires_http: z.boolean(),
    requires_http_write: z.boolean(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.requires_code !== value.code.length > 0) {
      context.addIssue({
        code: "custom",
        message: "requires_code must be derived from Code requirements",
        path: ["requires_code"],
      });
    }
    if (value.requires_http !== value.http.length > 0) {
      context.addIssue({
        code: "custom",
        message: "requires_http must be derived from HTTP requirements",
        path: ["requires_http"],
      });
    }
    const writeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);
    const requiresWrite = value.http.some((item) =>
      writeMethods.has(item.method),
    );
    if (value.requires_http_write !== requiresWrite) {
      context.addIssue({
        code: "custom",
        message: "requires_http_write must be derived from HTTP methods",
        path: ["requires_http_write"],
      });
    }
    const modelNodeIds = value.model_refs.map((item) => item.node_id);
    if (new Set(modelNodeIds).size !== modelNodeIds.length) {
      context.addIssue({
        code: "custom",
        message: "published Model refs must be unique per node",
        path: ["model_refs"],
      });
    }
    const slotIds = value.credential_slots.map((item) => item.slot_id);
    if (new Set(slotIds).size !== slotIds.length) {
      context.addIssue({
        code: "custom",
        message: "published Credential slots must be unique",
        path: ["credential_slots"],
      });
    }
  });

type GrantCoverage = {
  credential_slots: Array<
    z.infer<typeof workflowPublishedCredentialSlotV1Schema>
  >;
  missing_required_credential_slot_ids: string[];
  executable: boolean;
};

function addGrantCoverageIssues(
  value: GrantCoverage,
  context: z.RefinementCtx,
) {
  const required = new Set(
    value.credential_slots
      .filter((slot) => slot.required)
      .map((slot) => slot.slot_id),
  );
  const missing = new Set(value.missing_required_credential_slot_ids);
  if (missing.size !== value.missing_required_credential_slot_ids.length) {
    context.addIssue({
      code: "custom",
      message: "missing Credential slot IDs must be unique",
      path: ["missing_required_credential_slot_ids"],
    });
  }
  if ([...missing].some((slotId) => !required.has(slotId))) {
    context.addIssue({
      code: "custom",
      message: "missing Credential slots must be declared by the Version",
      path: ["missing_required_credential_slot_ids"],
    });
  }
  if (value.executable !== (missing.size === 0)) {
    context.addIssue({
      code: "custom",
      message: "Workflow Version executable state contradicts grant coverage",
      path: ["executable"],
    });
  }
}

const workflowVersionProjectionShape = {
  id: canonicalUuidSchema,
  workflow_id: canonicalUuidSchema,
  version_number: positiveSafeIntegerSchema,
  graph_schema_version: z.literal(1),
  canvas_schema_version: z.literal(1),
  compiler_contract_version: z.literal(1),
  semantic_checksum: sha256Schema,
  spec: workflowSpecV1Schema,
  canvas: canvasDocumentV1Schema,
  credential_slots: z.array(workflowPublishedCredentialSlotV1Schema).max(255),
  missing_required_credential_slot_ids: z
    .array(codePointBoundedString(1, 128))
    .max(255),
  executable: z.boolean(),
  published_at: timestampSchema,
};

export const workflowVersionResponseV1Schema = z
  .object(workflowVersionProjectionShape)
  .strict()
  .superRefine(addGrantCoverageIssues);

export const workflowVersionPageV1Schema = z
  .object({
    items: z.array(workflowVersionResponseV1Schema).max(100),
    next_cursor: responseCursorSchema.nullable(),
  })
  .strict();

export const workflowDraftGrantIntentResponseV1Schema = z
  .object({
    workflow_id: canonicalUuidSchema,
    slot_id: codePointBoundedString(1, 128),
    slot_schema_checksum: sha256Schema,
    credential_id: canonicalUuidSchema,
    expected_credential_version_id: canonicalUuidSchema,
    updated_at: timestampSchema,
  })
  .strict();

export const workflowDraftGrantIntentDeleteResponseV1Schema = z
  .object({
    workflow_id: canonicalUuidSchema,
    slot_id: codePointBoundedString(1, 128),
    deleted: z.literal(true),
  })
  .strict();

export const workflowCredentialGrantResponseV1Schema = z
  .object({
    workflow_id: canonicalUuidSchema,
    workflow_version_id: canonicalUuidSchema,
    slot_id: codePointBoundedString(1, 128),
    payload_schema_checksum: sha256Schema,
    credential_id: canonicalUuidSchema,
    credential_version_id: canonicalUuidSchema,
    status: z.enum(["active", "revoked"]),
    revision: positiveSafeIntegerSchema,
    created_at: timestampSchema,
    revoked_at: timestampSchema.nullable(),
  })
  .strict();

export const workflowDraftValidationResponseV1Schema = z
  .object({
    request_id: requestIdSchema,
    workflow_id: canonicalUuidSchema,
    draft_revision: positiveSafeIntegerSchema,
    draft_checksum: sha256Schema,
    valid: z.boolean(),
    issues: z.array(workflowValidationIssueV1Schema).max(1_024),
    semantic_checksum: sha256Schema.nullable(),
    requirements: workflowPublishedRequirementsV1Schema.nullable(),
    catalog_generation: sha256Schema.nullable(),
    policy_revision: positiveSafeIntegerSchema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    const successful =
      value.issues.length === 0 &&
      value.semantic_checksum !== null &&
      value.requirements !== null &&
      value.catalog_generation !== null &&
      value.policy_revision !== null;
    if (value.valid !== successful) {
      context.addIssue({
        code: "custom",
        message: "Workflow validation result fields contradict valid",
        path: ["valid"],
      });
    }
    if (
      !value.valid &&
      (value.issues.length === 0 ||
        value.semantic_checksum !== null ||
        value.requirements !== null)
    ) {
      context.addIssue({
        code: "custom",
        message: "invalid Workflow validation must carry only safe issues",
      });
    }
  });

const workflowPublishProjectionShape = {
  request_id: requestIdSchema,
  workflow_id: canonicalUuidSchema,
  version_id: canonicalUuidSchema,
  version_number: positiveSafeIntegerSchema,
  graph_schema_version: z.literal(1),
  canvas_schema_version: z.literal(1),
  compiler_contract_version: z.literal(1),
  semantic_checksum: sha256Schema,
  spec: workflowSpecV1Schema,
  canvas: canvasDocumentV1Schema,
  credential_slots: z.array(workflowPublishedCredentialSlotV1Schema).max(255),
  missing_required_credential_slot_ids: z
    .array(codePointBoundedString(1, 128))
    .max(255),
  executable: z.boolean(),
  published_at: timestampSchema,
};

export const workflowPublishResponseV1Schema = z
  .object(workflowPublishProjectionShape)
  .strict()
  .superRefine(addGrantCoverageIssues);

export type WorkflowDraftSaveRequestV1 = z.infer<
  typeof workflowDraftSaveRequestV1Schema
>;
export type WorkflowDraftValidateRequestV1 = z.infer<
  typeof workflowDraftValidateRequestV1Schema
>;
export type WorkflowPublishRequestV1 = z.infer<
  typeof workflowPublishRequestV1Schema
>;
export type WorkflowDefinitionCreateRequestV1 = z.infer<
  typeof workflowDefinitionCreateRequestV1Schema
>;
export type WorkflowDefinitionUpdateRequestV1 = z.infer<
  typeof workflowDefinitionUpdateRequestV1Schema
>;
export type WorkflowDefinitionArchiveRequestV1 = z.infer<
  typeof workflowDefinitionArchiveRequestV1Schema
>;
export type WorkflowDefinitionListQueryV1 = z.infer<
  typeof workflowDefinitionListQueryV1Schema
>;
export type WorkflowVersionListQueryV1 = z.infer<
  typeof workflowVersionListQueryV1Schema
>;
export type WorkflowCredentialGrantMutationRequestV1 = z.infer<
  typeof workflowCredentialGrantMutationRequestV1Schema
>;
export type WorkflowDefinitionResponseV1 = z.infer<
  typeof workflowDefinitionResponseV1Schema
>;
export type WorkflowDefinitionPageV1 = z.infer<
  typeof workflowDefinitionPageV1Schema
>;
export type WorkflowDraftResponseV1 = z.infer<
  typeof workflowDraftResponseV1Schema
>;
export type WorkflowDraftGrantIntentResponseV1 = z.infer<
  typeof workflowDraftGrantIntentResponseV1Schema
>;
export type WorkflowDraftGrantIntentDeleteResponseV1 = z.infer<
  typeof workflowDraftGrantIntentDeleteResponseV1Schema
>;
export type WorkflowVersionResponseV1 = z.infer<
  typeof workflowVersionResponseV1Schema
>;
export type WorkflowVersionPageV1 = z.infer<typeof workflowVersionPageV1Schema>;
export type WorkflowDraftValidationResponseV1 = z.infer<
  typeof workflowDraftValidationResponseV1Schema
>;
export type WorkflowPublishResponseV1 = z.infer<
  typeof workflowPublishResponseV1Schema
>;
export type WorkflowPublishedRequirementsV1 = z.infer<
  typeof workflowPublishedRequirementsV1Schema
>;
export type WorkflowPublishedModelRefV1 = z.infer<
  typeof workflowPublishedModelRefV1Schema
>;
export type WorkflowPublishedCredentialSlotV1 = z.infer<
  typeof workflowPublishedCredentialSlotV1Schema
>;
export type WorkflowPublishedCodeRequirementV1 = z.infer<
  typeof workflowPublishedCodeRequirementV1Schema
>;
export type WorkflowPublishedHttpRequirementV1 = z.infer<
  typeof workflowPublishedHttpRequirementV1Schema
>;
export type WorkflowCredentialGrantResponseV1 = z.infer<
  typeof workflowCredentialGrantResponseV1Schema
>;
