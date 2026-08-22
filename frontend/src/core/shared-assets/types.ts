import { z } from "zod";

import { capabilitySchema } from "@/core/projects/types";

export const ASSET_KINDS = ["agent", "skill", "mcp"] as const;
export const ASSET_LIST_KINDS = [
  "agents",
  "skills",
  "mcp-servers",
] as const;
export const ASSET_STATUSES = ["active", "archived", "suspended"] as const;
export const ASSET_WORKFLOW_STATUSES = [
  "draft",
  "pending_approval",
  "published",
  "rejected",
] as const;
export const VERSION_RELATIONS = [
  "current",
  "candidate",
  "historical",
] as const;
export const SKILL_SCAN_DECISIONS = ["allow", "warn", "block"] as const;
export const SKILL_FILE_PREVIEW_STATUSES = [
  "ready",
  "binary",
  "too_large",
] as const;
export const MCP_TRANSPORTS = [
  "stdio",
  "sse",
  "http",
  "streamable_http",
] as const;
export const MCP_TOOL_INVENTORY_STATUSES = [
  "never_discovered",
  "testing",
  "ready",
  "degraded",
  "failed",
  "stale",
] as const;
export const MCP_TOOL_DISCOVERY_ATTEMPT_STATUSES = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
] as const;
export const MCP_TOOL_INVENTORY_ERROR_CODES = [
  "mcp_discovery_unavailable",
  "mcp_catalog_invalid",
] as const;
export const AGENT_RUNTIME_ASSESSMENT_REASON_CODES = [
  "agent_unavailable",
  "runtime_dependency_unavailable",
  "model_unavailable",
] as const;
export const MAX_AGENT_RUNTIME_ASSESSMENTS = 100;
export const SECRET_PAYLOAD_GROUPS = [
  "env",
  "headers",
  "query",
  "oauth",
] as const;

export const assetKindSchema = z.enum(ASSET_KINDS);
export const assetListKindSchema = z.enum(ASSET_LIST_KINDS);
export const assetScopeSchema = z.enum(["system", "project"]);
export const assetStatusSchema = z.enum(ASSET_STATUSES);
export const assetWorkflowStatusSchema = z.enum(ASSET_WORKFLOW_STATUSES);
export const versionRelationSchema = z.enum(VERSION_RELATIONS);
export const skillScanDecisionSchema = z.enum(SKILL_SCAN_DECISIONS);
export const skillFilePreviewStatusSchema = z.enum(SKILL_FILE_PREVIEW_STATUSES);
export const mcpTransportSchema = z.enum(MCP_TRANSPORTS);
export const mcpToolInventoryStatusSchema = z.enum(MCP_TOOL_INVENTORY_STATUSES);
export const mcpToolDiscoveryAttemptStatusSchema = z.enum(
  MCP_TOOL_DISCOVERY_ATTEMPT_STATUSES,
);
export const mcpToolInventoryErrorCodeSchema = z.enum(
  MCP_TOOL_INVENTORY_ERROR_CODES,
);
export const secretPayloadGroupSchema = z.enum(SECRET_PAYLOAD_GROUPS);
export const agentRuntimeAssessmentReasonCodeSchema = z.enum(
  AGENT_RUNTIME_ASSESSMENT_REASON_CODES,
);
export const assetIdSchema = z.string().uuid();
export const assetCapabilitiesSchema = z.array(capabilitySchema);

export const assetSummarySchema = z
  .object({
    id: assetIdSchema,
    scope: assetScopeSchema,
    project_id: assetIdSchema.nullable(),
    slug: z.string().min(1),
    display_name: z.string().min(1),
    status: assetStatusSchema,
    current_published_version_id: assetIdSchema.nullable(),
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const currentVersionAssetSummarySchema = z
  .object({
    id: assetIdSchema,
    scope: assetScopeSchema,
    project_id: assetIdSchema.nullable(),
    slug: z.string().min(1),
    display_name: z.string().min(1),
    status: assetStatusSchema,
    current_version_id: assetIdSchema.nullable(),
    revision: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const legacyCompatibleAssetSummarySchema = z
  .union([currentVersionAssetSummarySchema, assetSummarySchema])
  .transform((value) => {
    if ("current_version_id" in value) return value;
    const { current_published_version_id, version, ...item } = value;
    return {
      ...item,
      current_version_id: current_published_version_id,
      revision: version,
    };
  });

const FORBIDDEN_RESPONSE_FIELDS = new Set([
  "plaintext",
  "ciphertext",
  "nonce",
  "key_id",
  "storage_locator",
  "secret_hash",
]);

function isSafeJsonValue(value: unknown): boolean {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return true;
  }
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isSafeJsonValue);
  if (typeof value !== "object") return false;
  return Object.entries(value).every(
    ([key, item]) =>
      !FORBIDDEN_RESPONSE_FIELDS.has(key) && isSafeJsonValue(item),
  );
}

function isStringMap(value: unknown): value is Record<string, string> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((item) => typeof item === "string")
  );
}

function isStringListMap(value: unknown): value is Record<string, string[]> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every(
      (item) =>
        Array.isArray(item) &&
        item.every((member) => typeof member === "string"),
    )
  );
}

const safeJsonObjectSchema = z.custom<Record<string, unknown>>(
  (value) =>
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    isSafeJsonValue(value),
  "Expected a safe JSON object",
);
const stringMapSchema = z.custom<Record<string, string>>(isStringMap);
const stringListMapSchema = z.custom<Record<string, string[]>>(isStringListMap);
const secretFieldNameSchema = z.string().min(1).max(255);
const secretFieldMapSchema = z
  .record(z.string())
  .superRefine((value, context) => {
    const fields = Object.keys(value);
    if (fields.length === 0) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Secret payload groups cannot be empty",
      });
    }
    for (const fieldName of fields) {
      if (!secretFieldNameSchema.safeParse(fieldName).success) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Secret field names must contain 1 to 255 characters",
          path: [fieldName],
        });
      }
    }
  });
const secretFieldListSchema = z
  .array(secretFieldNameSchema)
  .min(1)
  .refine((fields) => new Set(fields).size === fields.length, {
    message: "Secret field names must be unique within a group",
  });
export const agentModelSettingsSchema = z
  .object({
    temperature: z.number().min(0).max(2).optional(),
    max_tokens: z.number().int().min(1).max(200_000).optional(),
    thinking_enabled: z.boolean().optional(),
    reasoning_effort: z.enum(["low", "medium", "high"]).optional(),
  })
  .strict();

export const agentVersionSchema = z
  .object({
    id: assetIdSchema,
    agent_id: assetIdSchema,
    version_number: z.number().int().positive(),
    relation: versionRelationSchema,
    description: z.string(),
    agents_instructions: z.string(),
    soul: z.string(),
    identity: z.string(),
    user_context: z.string(),
    payload_schema_version: z.number().int().positive(),
    model_ref: z.string(),
    model_settings: agentModelSettingsSchema.optional(),
    tool_groups: z.array(z.string()),
    skill_refs: z.array(
      z.object({ scope: assetScopeSchema, asset_id: assetIdSchema }).strict(),
    ),
    mcp_version_ids: z.array(assetIdSchema),
    supersedes_version_id: assetIdSchema.nullable(),
    payload_checksum: z.string().min(1),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const skillSecretRequirementSchema = z
  .object({ name: z.string().min(1), optional: z.boolean() })
  .strict();
export const skillSecretDeclarationNameSchema = z
  .string()
  .min(1)
  .regex(/^[A-Za-z_][A-Za-z0-9_]*$/);
export const skillSecretNameSchema = skillSecretDeclarationNameSchema.max(255);
export const skillSecretStatusSchema = z
  .object({
    name: skillSecretNameSchema,
    optional: z.boolean(),
    configured: z.boolean(),
    revision: z.number().int().nonnegative(),
  })
  .strict();
export const skillSecretSetResponseSchema = z
  .object({
    skill_id: assetIdSchema,
    skill_version_id: assetIdSchema,
    revision: z.number().int().nonnegative(),
    readiness: z.enum(["ready", "unready"]),
    requirements: z.array(skillSecretStatusSchema),
    request_id: z.string().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    const names = value.requirements.map((requirement) => requirement.name);
    if (new Set(names).size !== names.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill secret names must be unique",
        path: ["requirements"],
      });
    }
    const ready = value.requirements.every(
      (requirement) => requirement.optional || requirement.configured,
    );
    if ((value.readiness === "ready") !== ready) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill secret readiness is inconsistent",
        path: ["readiness"],
      });
    }
  });
export const skillSecretReplaceInputSchema = z
  .object({
    secrets: z.record(skillSecretNameSchema, z.string().min(1)).refine(
      (value) => Object.keys(value).length > 0,
      "At least one Skill secret must be supplied",
    ),
  })
  .strict();
export const secretClearInputSchema = z.object({ confirmed: z.literal(true) }).strict();
const skillFileViewSchema = z
  .object({
    path: z.string().min(1),
    media_type: z.string().min(1),
    size_bytes: z.number().int().nonnegative(),
    sha256: z.string().min(1),
  })
  .strict();

export const skillFilePathSchema = z
  .string()
  .min(1)
  .max(512)
  .refine((path) => path === path.normalize("NFC"), {
    message: "Skill file paths must use canonical NFC",
  })
  .refine(
    (path) =>
      !path.startsWith("/") &&
      !path.includes("\\") &&
      !path.includes("\0") &&
      path
        .split("/")
        .every((part) => part !== "" && part !== "." && part !== ".."),
    { message: "Skill file paths must be relative POSIX paths" },
  );

export const skillVersionFileContentSchema = z
  .object({
    path: skillFilePathSchema,
    media_type: z.string().min(1),
    size_bytes: z.number().int().nonnegative(),
    sha256: z.string().min(1),
    preview_status: skillFilePreviewStatusSchema,
    encoding: z.literal("utf-8").nullable(),
    content: z.string().nullable(),
    source_payload_checksum: z.string().min(1),
    asset_revision: z.number().int().positive(),
  })
  .strict()
  .superRefine((value, context) => {
    const ready = value.preview_status === "ready";
    if (ready && (value.encoding !== "utf-8" || value.content === null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Ready Skill files require UTF-8 content",
      });
    }
    if (!ready && (value.encoding !== null || value.content !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Unavailable Skill files cannot include content",
      });
    }
  });

export const skillVersionFileContentResponseSchema = z
  .object({
    data: skillVersionFileContentSchema,
    request_id: z.string().min(1),
  })
  .strict();

const editableSkillFileChangeSchema = z
  .object({
    op: z.enum(["create", "replace"]),
    path: skillFilePathSchema,
    content: z.string().max(1024 * 1024),
    media_type: z.string().min(1).max(255),
  })
  .strict();
const deletedSkillFileChangeSchema = z
  .object({
    op: z.literal("delete"),
    path: skillFilePathSchema.refine((path) => path !== "SKILL.md", {
      message: "SKILL.md cannot be deleted",
    }),
  })
  .strict();

export const skillFileChangeSchema = z.union([
  editableSkillFileChangeSchema,
  deletedSkillFileChangeSchema,
]);
export const skillFileForkInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    expected_source_payload_checksum: z.string().min(1),
    changes: z.array(skillFileChangeSchema).min(1).max(256),
  })
  .strict()
  .refine(
    (value) =>
      new Set(value.changes.map((change) => change.path)).size ===
      value.changes.length,
    { message: "Skill file changes must use unique paths", path: ["changes"] },
  );

export const skillVersionSchema = z
  .object({
    id: assetIdSchema,
    skill_id: assetIdSchema,
    version_number: z.number().int().positive(),
    relation: versionRelationSchema,
    description: z.string(),
    frontmatter: safeJsonObjectSchema,
    compatibility: z.string().nullable(),
    secret_requirements: z.array(skillSecretRequirementSchema),
    scan_decision: skillScanDecisionSchema,
    scan_rule_ids: z.array(z.string()),
    scan_summary: safeJsonObjectSchema,
    file_views: z.array(skillFileViewSchema),
    supersedes_version_id: assetIdSchema.nullable(),
    payload_checksum: z.string().min(1),
    revoked_at: z.string().datetime({ offset: true }).nullable(),
    revoked_by_user_id: z.string().min(1).nullable(),
    revocation_reason_code: z
      .enum(["security", "policy", "integrity"])
      .nullable(),
    governance_status: z.enum(["active", "revoked"]),
    binding_eligible: z.boolean(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict()
  .superRefine((value, context) => {
    const revoked = value.governance_status === "revoked";
    const hasCompleteRevocation =
      value.revoked_at !== null &&
      value.revoked_by_user_id !== null &&
      value.revocation_reason_code !== null;
    const hasNoRevocation =
      value.revoked_at === null &&
      value.revoked_by_user_id === null &&
      value.revocation_reason_code === null;
    if ((revoked && !hasCompleteRevocation) || (!revoked && !hasNoRevocation)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Skill version revocation fields must be internally consistent",
        path: ["governance_status"],
      });
    }
    const bindingEligible = value.relation === "current" && !revoked;
    if (value.binding_eligible !== bindingEligible) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill version binding eligibility is inconsistent",
        path: ["binding_eligible"],
      });
    }
  });

const mcpSecretSlotNameSchema = z
  .string()
  .max(63)
  .regex(/^[a-z][a-z0-9._-]{0,62}$/);

const mcpSecretSlotSchema = z
  .object({
    id: assetIdSchema,
    name: mcpSecretSlotNameSchema,
    purpose: z.string(),
    payload_schema: stringListMapSchema,
    required: z.boolean(),
  })
  .strict();
const mcpDefinitionSlotSchema = z
  .object({
    name: mcpSecretSlotNameSchema,
    purpose: z.string(),
    payload_schema: stringListMapSchema,
    required: z.boolean(),
  })
  .strict();
const mcpDefinitionSchema = z
  .object({
    description: z.string(),
    transport: mcpTransportSchema,
    command: z.string().nullable(),
    args: z.array(z.string()),
    url: z.string().nullable(),
    env: stringMapSchema,
    headers: stringMapSchema,
    oauth: safeJsonObjectSchema,
    routing: safeJsonObjectSchema,
    tool_overrides: safeJsonObjectSchema,
    timeout_seconds: z.number().int().positive(),
    secret_slots: z.array(mcpDefinitionSlotSchema),
  })
  .strict();

export const mcpVersionSchema = z
  .object({
    id: assetIdSchema,
    mcp_server_id: assetIdSchema,
    version_number: z.number().int().positive(),
    workflow_status: assetWorkflowStatusSchema,
    definition: mcpDefinitionSchema,
    secret_slots: z.array(mcpSecretSlotSchema),
    supersedes_version_id: assetIdSchema.nullable(),
    payload_checksum: z.string().min(1),
    submitted_at: z.string().datetime({ offset: true }).nullable(),
    reviewed_at: z.string().datetime({ offset: true }).nullable(),
    reviewed_by_user_id: z.string().min(1).nullable(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const mcpToolSchema = z
  .object({
    name: z
      .string()
      .min(1)
      .max(255)
      .regex(/^[A-Za-z0-9_-]+$/),
    description: z.string().max(4096),
  })
  .strict();

export const mcpToolInventorySchema = z
  .object({
    status: mcpToolInventoryStatusSchema,
    tools: z.array(mcpToolSchema).max(128),
    last_attempt_at: z.string().datetime({ offset: true }).nullable(),
    last_success_at: z.string().datetime({ offset: true }).nullable(),
    error_code: mcpToolInventoryErrorCodeSchema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    const invalid = (message: string) =>
      context.addIssue({ code: "custom", message });
    if (
      new Set(value.tools.map((tool) => tool.name)).size !== value.tools.length
    ) {
      context.addIssue({
        code: "custom",
        message: "MCP tool names must be unique",
        path: ["tools"],
      });
    }
    if (
      value.status === "never_discovered" &&
      (value.tools.length > 0 ||
        value.last_attempt_at !== null ||
        value.last_success_at !== null ||
        value.error_code !== null)
    ) {
      invalid("Never-discovered MCP inventory cannot contain observations");
    }
    if (value.status === "testing" && value.error_code !== null) {
      invalid("Testing MCP inventory cannot contain an error");
    }
    if (
      value.status === "ready" &&
      (value.last_attempt_at === null ||
        value.last_success_at === null ||
        value.error_code !== null)
    ) {
      invalid("Ready MCP inventory requires a successful observation");
    }
    if (
      value.status === "degraded" &&
      (value.last_attempt_at === null ||
        value.last_success_at === null ||
        value.error_code === null)
    ) {
      invalid("Degraded MCP inventory requires success and failure metadata");
    }
    if (
      value.status === "failed" &&
      (value.tools.length > 0 ||
        value.last_attempt_at === null ||
        value.error_code === null)
    ) {
      invalid("Failed MCP inventory cannot expose stale tools");
    }
    if (
      value.status === "stale" &&
      (value.tools.length > 0 || value.error_code !== null)
    ) {
      invalid("Stale MCP inventory cannot expose another closure's tools");
    }
  });

export const mcpToolInventoryResponseSchema = z
  .object({
    data: mcpToolInventorySchema,
    request_id: z.string().min(1),
  })
  .strict();

export const mcpToolDiscoveryAttemptSchema = z
  .object({
    id: assetIdSchema,
    mcp_server_id: assetIdSchema,
    mcp_server_version_id: assetIdSchema,
    status: mcpToolDiscoveryAttemptStatusSchema,
    requested_at: z.string().datetime({ offset: true }),
    started_at: z.string().datetime({ offset: true }).nullable(),
    completed_at: z.string().datetime({ offset: true }).nullable(),
    error_code: mcpToolInventoryErrorCodeSchema.nullable(),
  })
  .strict();

export const mcpToolDiscoveryAttemptResponseSchema = z
  .object({
    data: mcpToolDiscoveryAttemptSchema,
    request_id: z.string().min(1),
  })
  .strict();

export const assetVersionSchema = z.union([
  agentVersionSchema,
  skillVersionSchema,
  mcpVersionSchema,
]);

export const agentVersionResponseSchema = z
  .object({ data: agentVersionSchema, request_id: z.string().min(1) })
  .strict();
export const skillVersionResponseSchema = z
  .object({ data: skillVersionSchema, request_id: z.string().min(1) })
  .strict();
export const mcpVersionResponseSchema = z
  .object({ data: mcpVersionSchema, request_id: z.string().min(1) })
  .strict();
const configuredMcpVersionSchema = mcpVersionSchema.extend({
  workflow_status: z.literal("published"),
});
export const configuredMcpResponseSchema = z
  .object({
    item: assetSummarySchema,
    version: configuredMcpVersionSchema,
    request_id: z.string().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.item.id !== value.version.mcp_server_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Configured MCP response asset and version must match",
      });
    }
    if (
      value.version.workflow_status === "published" &&
      value.item.current_published_version_id !== value.version.id
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Published configured MCP must be the current version",
        path: ["item", "current_published_version_id"],
      });
    }
  });

const projectMcpEditableVersionSchema = mcpVersionSchema.extend({
  workflow_status: z.enum(["published", "pending_approval"]),
});

function editableMcpSlotsMatch(
  version: z.infer<typeof projectMcpEditableVersionSchema>,
): boolean {
  if (
    version.definition.secret_slots.length !==
    version.secret_slots.length
  ) {
    return false;
  }
  return version.secret_slots.every((slot, index) => {
    const definitionSlot = version.definition.secret_slots[index];
    if (
      slot.name !== definitionSlot?.name ||
      slot.purpose !== definitionSlot.purpose ||
      slot.required !== definitionSlot.required
    ) {
      return false;
    }
    const groups = Object.keys(slot.payload_schema);
    const definitionGroups = Object.keys(definitionSlot.payload_schema);
    return (
      groups.length === definitionGroups.length &&
      groups.every((group) => {
        const fields = slot.payload_schema[group] ?? [];
        const definitionFields = definitionSlot.payload_schema[group] ?? [];
        return (
          fields.length === definitionFields.length &&
          fields.every(
            (field, fieldIndex) => field === definitionFields[fieldIndex],
          )
        );
      })
    );
  });
}

export const projectMcpEditableConfigurationResponseSchema = z
  .object({
    item: assetSummarySchema,
    version: projectMcpEditableVersionSchema,
    request_id: z.string().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      !projectConfiguredMcpDefinitionInputSchema.safeParse(
        value.version.definition,
      ).success ||
      !editableMcpSlotsMatch(value.version)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Editable MCP configuration must contain one safe Project definition",
        path: ["version", "definition"],
      });
    }
    if (
      value.item.scope !== "project" ||
      value.item.project_id === null ||
      value.item.id !== value.version.mcp_server_id
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Editable MCP configuration must belong to one Project asset",
        path: ["item", "id"],
      });
    }
    if (
      value.version.workflow_status === "published" &&
      value.item.current_published_version_id !== value.version.id
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Editable published MCP configuration must be the current version",
        path: ["item", "current_published_version_id"],
      });
    }
    if (
      value.version.workflow_status === "pending_approval" &&
      value.version.supersedes_version_id !==
        value.item.current_published_version_id
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Editable pending MCP configuration must extend the current published version",
        path: ["version", "supersedes_version_id"],
      });
    }
  });
export const versionResponseSchema = z
  .object({ data: assetVersionSchema, request_id: z.string().min(1) })
  .strict();
export const agentVersionHistoryResponseSchema = z
  .object({ data: z.array(agentVersionSchema), request_id: z.string().min(1) })
  .strict();
export const skillVersionHistoryResponseSchema = z
  .object({ data: z.array(skillVersionSchema), request_id: z.string().min(1) })
  .strict();
export const mcpVersionHistoryResponseSchema = z
  .object({ data: z.array(mcpVersionSchema), request_id: z.string().min(1) })
  .strict();
export const versionHistoryResponseSchema = z
  .object({ data: z.array(assetVersionSchema), request_id: z.string().min(1) })
  .strict();

export const agentRuntimeAssessmentsInputSchema = z
  .object({
    agent_ids: z
      .array(assetIdSchema)
      .min(1)
      .max(MAX_AGENT_RUNTIME_ASSESSMENTS)
      .refine((ids) => new Set(ids).size === ids.length, {
        message: "Agent runtime assessment IDs must be unique",
      }),
  })
  .strict();

export const agentRuntimeAssessmentSchema = z.discriminatedUnion(
  "reason_code",
  [
    z
      .object({
        agent_asset_id: assetIdSchema,
        selected_version_id: assetIdSchema,
        status: z.literal("ready"),
        reason_code: z.null(),
      })
      .strict(),
    z
      .object({
        agent_asset_id: assetIdSchema,
        selected_version_id: z.null(),
        status: z.literal("blocked"),
        reason_code: z.literal("agent_unavailable"),
      })
      .strict(),
    z
      .object({
        agent_asset_id: assetIdSchema,
        selected_version_id: assetIdSchema,
        status: z.literal("blocked"),
        reason_code: z.literal("runtime_dependency_unavailable"),
      })
      .strict(),
    z
      .object({
        agent_asset_id: assetIdSchema,
        selected_version_id: assetIdSchema,
        status: z.literal("blocked"),
        reason_code: z.literal("model_unavailable"),
      })
      .strict(),
  ],
);

export const agentRuntimeAssessmentsResponseSchema = z
  .object({
    items: z
      .array(agentRuntimeAssessmentSchema)
      .max(MAX_AGENT_RUNTIME_ASSESSMENTS),
    request_id: z.string().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      new Set(value.items.map((item) => item.agent_asset_id)).size !==
      value.items.length
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Agent runtime assessment items must be unique",
        path: ["items"],
      });
    }
  });

const legacySystemBindingItemSchema = z
  .object({
    project_id: assetIdSchema,
    kind: assetKindSchema,
    asset_id: assetIdSchema,
    version_id: assetIdSchema,
    enabled: z.boolean(),
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    updated_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

const currentSystemBindingItemSchema = z
  .object({
    project_id: assetIdSchema,
    kind: assetKindSchema,
    asset_id: assetIdSchema,
    current_version_id: assetIdSchema,
    enabled: z.boolean(),
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    updated_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

const systemBindingItemSchema = z
  .union([currentSystemBindingItemSchema, legacySystemBindingItemSchema])
  .transform((value) => {
    if ("current_version_id" in value) return value;
    const { version_id, ...binding } = value;
    return { ...binding, current_version_id: version_id };
  });

export const systemBindingSchema = z
  .union([
    currentSystemBindingItemSchema
      .extend({ request_id: z.string().min(1) })
      .strict(),
    legacySystemBindingItemSchema
      .extend({ request_id: z.string().min(1) })
      .strict(),
  ])
  .transform((value) => {
    if ("current_version_id" in value) return value;
    const { version_id, ...binding } = value;
    return { ...binding, current_version_id: version_id };
  });

export const currentSystemBindingSchema = currentSystemBindingItemSchema
  .extend({ request_id: z.string().min(1) })
  .strict();

const legacyProjectAssetItemSchema = assetSummarySchema
  .extend({
    capabilities: assetCapabilitiesSchema,
    binding: systemBindingItemSchema.nullable(),
    description: z.string().nullable().optional(),
  })
  .strict();

export const currentVersionProjectAssetItemSchema =
  currentVersionAssetSummarySchema
    .extend({
      capabilities: assetCapabilitiesSchema,
      binding: currentSystemBindingItemSchema.nullable(),
      description: z.string().nullable().optional(),
    })
    .strict();

export const legacyCompatibleProjectAssetItemSchema = z
  .union([currentVersionProjectAssetItemSchema, legacyProjectAssetItemSchema])
  .transform((value) => {
    if ("current_version_id" in value) return value;
    const { current_published_version_id, version, ...item } = value;
    return {
      ...item,
      current_version_id: current_published_version_id,
      revision: version,
    };
  });

export const projectAssetItemSchema = currentVersionProjectAssetItemSchema;

export const projectAssetListSchema = z
  .object({
    system_items: z.array(projectAssetItemSchema),
    project_items: z.array(projectAssetItemSchema),
    request_id: z.string().min(1),
  })
  .strict();

export const legacyCompatibleProjectAssetListSchema = z
  .object({
    system_items: z.array(legacyCompatibleProjectAssetItemSchema),
    project_items: z.array(legacyCompatibleProjectAssetItemSchema),
    request_id: z.string().min(1),
  })
  .strict();

export const adminAssetListSchema = z
  .object({
    items: z.array(currentVersionAssetSummarySchema),
    request_id: z.string().min(1),
  })
  .strict();

export const legacyCompatibleAdminAssetListSchema = z
  .object({
    items: z.array(legacyCompatibleAssetSummarySchema),
    request_id: z.string().min(1),
  })
  .strict();

export const assetMutationResponseSchema = z
  .object({
    item: currentVersionAssetSummarySchema,
    request_id: z.string().min(1),
  })
  .strict();

export const legacyCompatibleAssetMutationResponseSchema = z
  .object({
    item: legacyCompatibleAssetSummarySchema,
    request_id: z.string().min(1),
  })
  .strict();

export const projectSkillImportResponseSchema = z
  .object({
    item: currentVersionAssetSummarySchema,
    version: skillVersionSchema,
    request_id: z.string().min(1),
  })
  .strict();

export const createAgentInputSchema = z
  .object({
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
    description: z.string(),
    agents_instructions: z.string(),
    soul: z.string(),
    identity: z.string(),
    user_context: z.string(),
    model_ref: z.string().trim().min(1),
    model_settings: agentModelSettingsSchema,
    tool_groups: z.array(z.string().trim().min(1)),
    skill_refs: z.array(
      z.object({ scope: assetScopeSchema, asset_id: assetIdSchema }).strict(),
    ),
    mcp_version_ids: z.array(assetIdSchema),
  })
  .strict();

export const agentCreateResponseSchema = z
  .object({
    item: currentVersionAssetSummarySchema,
    version: agentVersionSchema,
    request_id: z.string().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    const invalid = (path: (string | number)[], message: string) =>
      context.addIssue({ code: z.ZodIssueCode.custom, path, message });
    if (value.item.id !== value.version.agent_id) {
      invalid(
        ["version", "agent_id"],
        "Created Agent version must belong to the returned asset",
      );
    }
    if (value.item.scope !== "project" || value.item.project_id === null) {
      invalid(["item", "scope"], "Created Agent must be project scoped");
    }
    if (
      value.item.status !== "suspended" ||
      value.item.current_version_id !== null
    ) {
      invalid(
        ["item", "status"],
        "Created Agent must start suspended without a live version",
      );
    }
    if (
      value.version.version_number !== 1 ||
      value.version.relation !== "candidate" ||
      value.version.supersedes_version_id !== null
    ) {
      invalid(
        ["version"],
        "Created Agent must return an initial standalone Candidate Version",
      );
    }
  });

export const expectedAssetVersionInputSchema = z
  .object({ expected_asset_version: z.number().int().positive() })
  .strict();
export const expectedRevisionInputSchema = z
  .object({ expected_revision: z.number().int().positive() })
  .strict();
export const skillActivationInputSchema = expectedRevisionInputSchema
  .extend({
    expected_payload_checksum: z.string().regex(/^[a-f0-9]{64}$/u),
    expected_secret_revision: z.number().int().nonnegative(),
  })
  .strict();
export const projectDefaultAgentSchema = z
  .object({
    agent_asset_id: assetIdSchema.nullable(),
    revision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    request_id: z.string().min(1),
  })
  .strict();
export const projectDefaultAgentInputSchema = z
  .object({
    agent_asset_id: assetIdSchema.nullable(),
    expected_revision: z
      .number()
      .int()
      .nonnegative()
      .max(Number.MAX_SAFE_INTEGER),
  })
  .strict();

export const agentInstructionsInputSchema = z
  .object({
    agents_instructions: z.string(),
    soul: z.string(),
    identity: z.string(),
    user_context: z.string(),
    expected_revision: z.number().int().positive(),
  })
  .strict();
export const agentCapabilityBindingsInputSchema = z
  .object({
    skill_refs: z.array(
      z.object({ scope: assetScopeSchema, asset_id: assetIdSchema }).strict(),
    ),
    mcp_version_ids: z.array(assetIdSchema),
    expected_revision: z.number().int().positive(),
  })
  .strict()
  .refine(
    (value) =>
      new Set(value.skill_refs.map((item) => `${item.scope}:${item.asset_id}`))
        .size === value.skill_refs.length &&
      new Set(value.mcp_version_ids).size === value.mcp_version_ids.length,
    { message: "Agent capability version IDs must be unique" },
  );
export const skillVersionInputSchema = z
  .object({
    files: z
      .array(
        z
          .object({
            path: z.string().min(1),
            content_base64: z.string().min(1),
            media_type: z.string().min(1).default("application/octet-stream"),
          })
          .strict(),
      )
      .min(1),
    expected_revision: z.number().int().positive(),
  })
  .strict();
const mcpSecretSlotInputSchema = z
  .object({
    name: mcpSecretSlotNameSchema,
    purpose: z.string().default(""),
    payload_schema: stringListMapSchema,
    required: z.boolean().default(true),
  })
  .strict();

const mcpDefinitionInputCommonShape = {
  description: z.string().default(""),
  command: z.string().nullable().default(null),
  args: z.array(z.string()).default([]),
  url: z.string().nullable().default(null),
  env: stringMapSchema.default({}),
  headers: stringMapSchema.default({}),
  oauth: safeJsonObjectSchema.default({}),
  routing: safeJsonObjectSchema.default({}),
  tool_overrides: safeJsonObjectSchema.default({}),
  timeout_seconds: z.number().int().positive().default(30),
};

export const mcpVersionInputSchema = z
  .object({
    ...mcpDefinitionInputCommonShape,
    transport: mcpTransportSchema.default("stdio"),
    secret_slots: z.array(mcpSecretSlotInputSchema).default([]),
    expected_asset_version: z.number().int().positive(),
  })
  .strict();

function configuredProjectMcpRawHostname(value: string): string | null {
  const authority = value.slice(value.indexOf("://") + 3).split("/", 1)[0];
  if (!authority || authority.includes("@")) return null;
  if (authority.startsWith("[")) {
    const closingBracket = authority.indexOf("]");
    if (closingBracket < 0) return null;
    const remainder = authority.slice(closingBracket + 1);
    if (remainder && !/^:\d+$/u.test(remainder)) return null;
    return authority.slice(0, closingBracket + 1);
  }
  const portSeparator = authority.lastIndexOf(":");
  if (portSeparator < 0) return authority;
  if (!/^\d+$/u.test(authority.slice(portSeparator + 1))) return null;
  return authority.slice(0, portSeparator);
}

export function isSafeConfiguredProjectMcpUrl(value: string | null): boolean {
  if (
    !value?.trim() ||
    value !== value.trim() ||
    value.includes("\\") ||
    value.includes("?") ||
    value.includes("#") ||
    /[\u0000-\u0020\u007f]/u.test(value) ||
    !/^https?:\/\/[^/\\?#]+/iu.test(value)
  ) {
    return false;
  }
  const rawHostname = configuredProjectMcpRawHostname(value);
  if (!rawHostname) return false;
  try {
    const parsed = new URL(value);
    const isExactLocalhost = rawHostname.toLowerCase() === "localhost";
    const isCanonicalIpv4 = /^\d+(?:\.\d+){3}$/u.test(rawHostname);
    const isCanonicalIpv6 =
      rawHostname.startsWith("[") && rawHostname.endsWith("]");
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      !parsed.username &&
      !parsed.password &&
      (isExactLocalhost
        ? parsed.hostname === "localhost"
        : rawHostname === parsed.hostname) &&
      (isExactLocalhost || isCanonicalIpv4 || isCanonicalIpv6) &&
      parsed.port !== "0" &&
      !rawHostname.includes("*")
    );
  } catch {
    return false;
  }
}

const projectConfiguredMcpSecretPayloadSchema = z
  .object({
    headers: secretFieldListSchema.optional(),
    query: secretFieldListSchema.optional(),
  })
  .strict()
  .refine((value) => Object.keys(value).length === 1, {
    message:
      "Configured project MCP secret slots support exactly one headers or query group",
  });

const projectConfiguredMcpSecretSlotInputSchema =
  mcpSecretSlotInputSchema.extend({
    payload_schema: projectConfiguredMcpSecretPayloadSchema,
  });

const projectConfiguredMcpDefinitionObjectSchema = z
  .object({
    ...mcpDefinitionInputCommonShape,
    transport: z.enum(["sse", "http"]).default("http"),
    secret_slots: z
      .array(projectConfiguredMcpSecretSlotInputSchema)
      .default([]),
  })
  .strict();

type ProjectConfiguredMcpDefinitionInput = z.infer<
  typeof projectConfiguredMcpDefinitionObjectSchema
>;

function validateProjectConfiguredMcpDefinition(
  value: ProjectConfiguredMcpDefinitionInput,
  context: z.RefinementCtx,
): void {
  if (!isSafeConfiguredProjectMcpUrl(value.url)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message:
        "Configured project MCP URL must use localhost or a canonical IPv4/IPv6 literal without credentials, query parameters, or fragments",
      path: ["url"],
    });
  }
  const slotNames = value.secret_slots.map((slot) => slot.name);
  if (new Set(slotNames).size !== slotNames.length) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "Configured project MCP secret slot names must be unique",
      path: ["secret_slots"],
    });
  }
}

export const projectConfiguredMcpDefinitionInputSchema =
  projectConfiguredMcpDefinitionObjectSchema.superRefine(
    validateProjectConfiguredMcpDefinition,
  );

export const createConfiguredMcpInputSchema =
  projectConfiguredMcpDefinitionObjectSchema
    .extend({
      slug: z
        .string()
        .trim()
        .min(3)
        .max(63)
        .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/u),
      display_name: z.string().trim().min(1).max(120),
    })
    .strict()
    .superRefine(validateProjectConfiguredMcpDefinition);

export const updateConfiguredMcpInputSchema =
  projectConfiguredMcpDefinitionObjectSchema
    .extend({ expected_asset_version: z.number().int().positive() })
    .strict()
    .superRefine(validateProjectConfiguredMcpDefinition);

export const syncCurrentSystemMcpBindingInputSchema = z
  .object({
    expected_binding_version: z.number().int().positive().optional(),
  })
  .strict();

export const mcpSecretPayloadSchema = z
  .object({
    env: secretFieldMapSchema.optional(),
    headers: secretFieldMapSchema.optional(),
    query: secretFieldMapSchema.optional(),
    oauth: secretFieldMapSchema.optional(),
  })
  .strict()
  .refine((value) => Object.keys(value).length > 0, {
    message: "MCP secret payload cannot be empty",
  });
export const mcpSecretReplaceInputSchema = z
  .object({
    payload: mcpSecretPayloadSchema,
  })
  .strict();
export const mcpSecretSlotStatusSchema = mcpSecretSlotSchema
  .extend({
    configured: z.boolean(),
    revision: z.number().int().nonnegative(),
  })
  .strict();
export const mcpSecretSetResponseSchema = z
  .object({
    mcp_server_id: assetIdSchema,
    mcp_server_version_id: assetIdSchema,
    revision: z.number().int().nonnegative(),
    readiness: z.enum(["ready", "unready"]),
    slots: z.array(mcpSecretSlotStatusSchema),
    request_id: z.string().min(1),
  })
  .strict();
export const enableSystemBindingInputSchema = z
  .object({
    asset_id: assetIdSchema,
    version_id: assetIdSchema,
    expected_binding_version: z.number().int().positive().optional(),
  })
  .strict();
export const enableCurrentSystemBindingInputSchema = z
  .object({
    asset_id: assetIdSchema,
    expected_binding_version: z.number().int().positive().optional(),
  })
  .strict();
export const moveSystemBindingInputSchema = z
  .object({
    version_id: assetIdSchema,
    expected_binding_version: z.number().int().positive(),
  })
  .strict();
export const disableSystemBindingInputSchema = z
  .object({ expected_binding_version: z.number().int().positive() })
  .strict();

export type AssetKind = z.infer<typeof assetKindSchema>;
export type AssetListKind = z.infer<typeof assetListKindSchema>;
export type MutableAssetListKind = AssetListKind;
export type ProjectAssetStatusAction<Kind extends MutableAssetListKind> =
  Kind extends "skills" | "agents"
    ? "enable" | "suspend"
    : "activate" | "suspend";
export type AdminProjectAssetStatusAction<Kind extends MutableAssetListKind> =
  Kind extends "skills" | "agents"
    ? "enable" | "suspend"
    : "archive" | "suspend";
export type AssetScope = z.infer<typeof assetScopeSchema>;
export type AssetStatus = z.infer<typeof assetStatusSchema>;
export type SecretPayloadGroup = z.infer<typeof secretPayloadGroupSchema>;
export type AssetSummary = z.infer<typeof currentVersionAssetSummarySchema>;
export type ProjectAssetItem = z.infer<typeof projectAssetItemSchema>;
export type AssetVersion = z.infer<typeof assetVersionSchema>;
export type AgentVersion = z.infer<typeof agentVersionSchema>;
export type SkillVersion = z.infer<typeof skillVersionSchema>;
export type McpVersion = z.infer<typeof mcpVersionSchema>;
export type McpTool = z.infer<typeof mcpToolSchema>;
export type McpToolInventory = z.infer<typeof mcpToolInventorySchema>;
export type McpToolInventoryResponse = z.infer<
  typeof mcpToolInventoryResponseSchema
>;
export type McpToolDiscoveryAttempt = z.infer<
  typeof mcpToolDiscoveryAttemptSchema
>;
export type McpToolDiscoveryAttemptResponse = z.infer<
  typeof mcpToolDiscoveryAttemptResponseSchema
>;
export type VersionResponse = z.infer<typeof versionResponseSchema>;
export type AgentVersionResponse = z.infer<typeof agentVersionResponseSchema>;
export type McpVersionResponse = z.infer<typeof mcpVersionResponseSchema>;
export type ConfiguredMcpResponse = z.infer<typeof configuredMcpResponseSchema>;
export type ProjectMcpEditableConfigurationResponse = z.infer<
  typeof projectMcpEditableConfigurationResponseSchema
>;
export type VersionHistoryResponse = z.infer<
  typeof versionHistoryResponseSchema
>;
export type AgentRuntimeAssessmentReasonCode = z.infer<
  typeof agentRuntimeAssessmentReasonCodeSchema
>;
export type AgentRuntimeAssessment = z.infer<
  typeof agentRuntimeAssessmentSchema
>;
export type AgentRuntimeAssessmentsInput = z.input<
  typeof agentRuntimeAssessmentsInputSchema
>;
export type AgentRuntimeAssessmentsResponse = z.infer<
  typeof agentRuntimeAssessmentsResponseSchema
>;
export type SystemBinding = z.infer<typeof systemBindingSchema>;
export type ProjectAssetList = z.infer<typeof projectAssetListSchema>;
export type ProjectDefaultAgent = z.infer<typeof projectDefaultAgentSchema>;
export type AdminAssetList = z.infer<typeof adminAssetListSchema>;
export type AssetMutationResponse = z.infer<typeof assetMutationResponseSchema>;
export type ProjectSkillImportResponse = z.infer<
  typeof projectSkillImportResponseSchema
>;
export type CreateAgentInput = z.input<typeof createAgentInputSchema>;
export type AgentCreateResponse = z.infer<typeof agentCreateResponseSchema>;
export type AgentInstructionsInput = z.input<
  typeof agentInstructionsInputSchema
>;
export type AgentCapabilityBindingsInput = z.input<
  typeof agentCapabilityBindingsInputSchema
>;
export type SkillVersionInput = z.input<typeof skillVersionInputSchema>;
export type SkillVersionFileContent = z.infer<
  typeof skillVersionFileContentSchema
>;
export type SkillVersionFileContentResponse = z.infer<
  typeof skillVersionFileContentResponseSchema
>;
export type SkillFileChange = z.input<typeof skillFileChangeSchema>;
export type SkillFileForkInput = z.input<typeof skillFileForkInputSchema>;
export type SkillSecretStatus = z.infer<typeof skillSecretStatusSchema>;
export type SkillSecretSetResponse = z.infer<
  typeof skillSecretSetResponseSchema
>;
export type SkillSecretReplaceInput = z.input<
  typeof skillSecretReplaceInputSchema
>;
export type McpVersionInput = z.input<typeof mcpVersionInputSchema>;
export type CreateConfiguredMcpInput = z.input<
  typeof createConfiguredMcpInputSchema
>;
export type UpdateConfiguredMcpInput = z.input<
  typeof updateConfiguredMcpInputSchema
>;
export type SyncCurrentSystemMcpBindingInput = z.input<
  typeof syncCurrentSystemMcpBindingInputSchema
>;
export type ExpectedAssetVersionInput = z.input<
  typeof expectedAssetVersionInputSchema
>;
export type ExpectedRevisionInput = z.input<typeof expectedRevisionInputSchema>;
export type SkillActivationInput = z.input<typeof skillActivationInputSchema>;
export type ProjectDefaultAgentInput = z.input<
  typeof projectDefaultAgentInputSchema
>;
export type McpSecretPayload = z.infer<typeof mcpSecretPayloadSchema>;
export type McpSecretReplaceInput = z.input<
  typeof mcpSecretReplaceInputSchema
>;
export type McpSecretSetResponse = z.infer<typeof mcpSecretSetResponseSchema>;
export type SecretClearInput = z.input<typeof secretClearInputSchema>;
export type EnableSystemBindingInput = z.input<
  typeof enableSystemBindingInputSchema
>;
export type EnableCurrentSystemBindingInput = z.input<
  typeof enableCurrentSystemBindingInputSchema
>;
export type MoveSystemBindingInput = z.input<
  typeof moveSystemBindingInputSchema
>;
export type DisableSystemBindingInput = z.input<
  typeof disableSystemBindingInputSchema
>;
