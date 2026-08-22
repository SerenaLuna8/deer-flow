import { z } from "zod";

import {
  currentVersionAssetSummarySchema,
  skillVersionSchema,
} from "@/core/shared-assets/types";

const uuidSchema = z.string().uuid();
const timestampSchema = z.string().datetime({ offset: true });
const checksumSchema = z.string().regex(/^[a-f0-9]{64}$/u);
const idempotencyKeySchema = z
  .string()
  .trim()
  .min(1)
  .max(255)
  .refine((value) => !value.includes("\0"));
export const SKILL_BUILDER_MAX_MESSAGE_CHARS = 8_000;
export const SKILL_BUILDER_MAX_FILES = 128;
export const SKILL_BUILDER_MAX_FILE_BYTES = 512 * 1024;
export const SKILL_BUILDER_MAX_TOTAL_BYTES = 2 * 1024 * 1024;
export const SKILL_BUILDER_MAX_ATTACHMENTS = 4;
export const SKILL_BUILDER_MAX_ATTACHMENT_BYTES = 256 * 1024;
export const SKILL_BUILDER_MAX_ATTACHMENTS_TOTAL_BYTES = 512 * 1024;

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

export const skillBuilderStatusSchema = z.enum([
  "interviewing",
  "generating",
  "awaiting_clarification",
  "draft_ready",
  "validated",
  "committing",
  "completed",
  "failed",
  "cancelled",
]);

export const skillBuilderSessionKindSchema = z.enum(["create", "revise"]);

export const skillBuilderReasoningEffortSchema = z.enum([
  "none",
  "low",
  "medium",
  "high",
]);

const skillBuilderExecutionModelNameSchema = z
  .string()
  .regex(/^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$/u);

export const skillBuilderExecutionPreferenceSchema = z
  .object({
    model_name: skillBuilderExecutionModelNameSchema,
    mode: z.enum(["flash", "thinking", "pro", "ultra"]),
    thinking_enabled: z.boolean(),
    reasoning_effort: skillBuilderReasoningEffortSchema.nullable(),
  })
  .strict();

export const skillBuilderProgressStatusSchema = z.enum([
  "pending",
  "running",
  "completed",
  "failed",
]);

export const skillBuilderProgressItemSchema = z
  .object({
    id: z.string().trim().min(1),
    label: z.string().trim().min(1),
    status: skillBuilderProgressStatusSchema,
  })
  .strict();

export const skillBuilderMessageSchema = z
  .object({
    id: z.string().trim().min(1),
    role: z.enum(["user", "assistant"]),
    content: z.string(),
    created_at: timestampSchema,
    operation_id: uuidSchema.nullish(),
  })
  .strict();

const skillBuilderActivityBaseSchema = z.object({
  seq: z.string().regex(/^(0|[1-9][0-9]*)$/u),
  operation_id: uuidSchema,
  run_id: z.string().trim().min(1).max(64).nullable(),
  attempt: z.number().int().positive().nullable(),
  created_at: timestampSchema,
});

const skillBuilderEmptyActivityKinds = [
  "request_accepted",
  "attempt_started",
  "candidate_generated",
  "validation_passed",
  "validation_failed",
  "repair_started",
  "commit_accepted",
  "commit_validation_started",
  "commit_validation_passed",
  "commit_persistence_started",
  "commit_persistence_completed",
] as const;

const skillBuilderEmptyActivitySchema = skillBuilderActivityBaseSchema
  .extend({
    kind: z.enum(skillBuilderEmptyActivityKinds),
    payload: z.object({}).strict(),
  })
  .strict();

const skillBuilderValidationStartedActivitySchema =
  skillBuilderActivityBaseSchema
    .extend({
      kind: z.literal("validation_started"),
      payload: z.union([
        z.object({}).strict(),
        z
          .object({
            stage: z.enum(["package_files", "safety_scan"]),
          })
          .strict(),
      ]),
    })
    .strict();

const skillBuilderReasoningActivitySchema = skillBuilderActivityBaseSchema
  .extend({
    kind: z.literal("reasoning"),
    payload: z.object({ text: z.string().min(1) }).strict(),
  })
  .strict();

const skillBuilderToolDetailFields: Record<string, ReadonlySet<string>> = {
  search_available_skills: new Set(["result_count"]),
  read_skill_version: new Set(["resource_name"]),
  search_available_mcp_tools: new Set(["result_count"]),
  inspect_mcp_tool: new Set(["resource_name"]),
  list_candidate_files: new Set(["result_count"]),
  read_candidate_file: new Set(["path", "size_bytes"]),
  upsert_candidate_file: new Set(["path", "size_bytes"]),
  delete_candidate_file: new Set(["path", "size_bytes"]),
  request_skill_clarification: new Set<string>(),
  finalize_skill_candidate: new Set<string>(),
} as const;

const skillBuilderToolActivitySchema = skillBuilderActivityBaseSchema
  .extend({
    kind: z.enum(["tool_started", "tool_completed", "tool_failed"]),
    payload: z
      .object({
        tool_call_id: z.string().trim().min(1).max(512),
        tool_name: z.enum([
          "search_available_skills",
          "read_skill_version",
          "search_available_mcp_tools",
          "inspect_mcp_tool",
          "list_candidate_files",
          "read_candidate_file",
          "upsert_candidate_file",
          "delete_candidate_file",
          "request_skill_clarification",
          "finalize_skill_candidate",
        ]),
        result_count: z.number().int().min(0).max(128).nullish(),
        resource_name: z.string().trim().min(1).max(512).nullish(),
        path: z
          .string()
          .trim()
          .min(1)
          .max(1_024)
          .refine(
            (value) =>
              !value.startsWith("/") && !value.split("/").includes(".."),
          )
          .nullish(),
        size_bytes: z
          .number()
          .int()
          .min(0)
          .max(2 * 1024 * 1024)
          .nullish(),
      })
      .strict()
      .superRefine((payload, context) => {
        const allowed = skillBuilderToolDetailFields[payload.tool_name];
        if (!allowed) return;
        for (const field of [
          "result_count",
          "resource_name",
          "path",
          "size_bytes",
        ] as const) {
          if (payload[field] != null && !allowed.has(field)) {
            context.addIssue({
              code: "custom",
              path: [field],
              message: "field is not public for this tool",
            });
          }
        }
      }),
  })
  .strict();

const skillBuilderTerminalActivitySchema = skillBuilderActivityBaseSchema
  .extend({
    kind: z.enum(["run_terminal", "commit_terminal"]),
    payload: z
      .object({
        status: z.enum(["completed", "failed", "stopped"]),
        code: z.string().trim().min(1).max(64).nullish(),
      })
      .strict(),
  })
  .strict();

export const skillBuilderActivitySchema = z.union([
  skillBuilderEmptyActivitySchema,
  skillBuilderValidationStartedActivitySchema,
  skillBuilderReasoningActivitySchema,
  skillBuilderToolActivitySchema,
  skillBuilderTerminalActivitySchema,
]);

export function skillBuilderActivityTerminal(
  activities: readonly SkillBuilderActivity[],
): {
  activity: SkillBuilderActivity;
  status: "completed" | "failed" | "stopped";
} | null {
  const terminal = [...activities]
    .reverse()
    .find(
      (activity) =>
        activity.kind === "run_terminal" || activity.kind === "commit_terminal",
    );
  if (
    terminal &&
    (terminal.kind === "run_terminal" || terminal.kind === "commit_terminal")
  ) {
    return { activity: terminal, status: terminal.payload.status };
  }
  const last = activities.at(-1);
  if (
    last?.run_id === null &&
    !activities.some((activity) => activity.run_id !== null) &&
    (last.kind === "validation_passed" || last.kind === "validation_failed")
  ) {
    return {
      activity: last,
      status: last.kind === "validation_passed" ? "completed" : "failed",
    };
  }
  return null;
}

export const skillBuilderActivityListResponseSchema = z
  .object({
    data: z.array(skillBuilderActivitySchema),
    request_id: z.string().trim().min(1),
  })
  .strict();

const clarificationOptionSchema = z
  .object({
    id: z.string().trim().min(1),
    label: z.string().trim().min(1),
    value: z.string(),
  })
  .strict();

export const skillBuilderClarificationRequestSchema = z
  .object({
    version: z.literal(1),
    kind: z.literal("human_input_request"),
    source: z.string().trim().min(1),
    request_id: z.string().trim().min(1),
    clarification_type: z.string().trim().min(1),
    title: z.string().trim().min(1),
    question: z.string().trim().min(1),
    context: z.string().trim().min(1),
    input_mode: z.enum(["free_text", "single_choice", "choice_with_other"]),
    options: z.array(clarificationOptionSchema),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.input_mode !== "free_text" &&
      (!value.options || value.options.length === 0)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Choice input requires at least one option",
        path: ["options"],
      });
    }
  });

/**
 * Canonical durable Run states. The Builder stream adapter projects its
 * provider-specific events into this small, public lifecycle before UI code
 * sees them.
 */
export const skillBuilderRunStatusSchema = z.enum([
  "pending",
  "running",
  "success",
  "error",
  "timeout",
  "interrupted",
]);

export const skillBuilderRunAdmissionStatusSchema = z.enum([
  "pending",
  "running",
]);

export const skillBuilderRunAdmissionSchema = z
  .object({
    runId: uuidSchema,
    status: skillBuilderRunAdmissionStatusSchema,
    streamUrl: z
      .string()
      .trim()
      .min(1)
      .max(2048)
      .refine((value) => !/[\u0000-\u001f]/u.test(value)),
  })
  .strict();

export const skillBuilderRunToolStepStatusSchema = z.enum([
  "pending",
  "running",
  "completed",
  "failed",
]);

/**
 * Safe UI projection for one tool step. Raw arguments and results are
 * deliberately absent; a future stream adapter must reduce them to a trusted
 * display name before parsing this contract.
 */
export const skillBuilderRunToolStepProjectionSchema = z
  .object({
    id: z.string().trim().min(1),
    toolName: z.string().trim().min(1).max(255),
    status: skillBuilderRunToolStepStatusSchema,
  })
  .strict();

/**
 * Builder-owned stream projection. This is intentionally not the raw SSE
 * wire shape: the eventual stream adapter is responsible for ordering and
 * reducing durable events into this strict, secret-free view.
 */
export const skillBuilderRunStreamProjectionSchema = z
  .object({
    runId: uuidSchema,
    status: skillBuilderRunStatusSchema,
    messages: z.array(skillBuilderMessageSchema),
    toolSteps: z.array(skillBuilderRunToolStepProjectionSchema),
    clarification: skillBuilderClarificationRequestSchema.nullable(),
  })
  .strict();

export const skillBuilderFilePathSchema = z
  .string()
  .min(1)
  .max(1024)
  .refine(
    (path) =>
      path === path.trim() &&
      !path.startsWith("/") &&
      !path.includes("\\") &&
      !path.includes("\0") &&
      path.normalize("NFC") === path &&
      path
        .split("/")
        .every((part) => part !== "" && part !== "." && part !== ".."),
    "Skill file path must be a safe relative POSIX path",
  );

export const skillBuilderFileSchema = z
  .object({
    path: skillBuilderFilePathSchema,
    media_type: z.string().trim().min(1).max(255),
    size_bytes: z.number().int().nonnegative(),
    sha256: z.string().regex(/^[a-f0-9]{64}$/u),
    encoding: z.literal("utf-8"),
    content: z.string(),
  })
  .strict()
  .superRefine((value, context) => {
    const contentBytes = utf8ByteLength(value.content);
    if (contentBytes > SKILL_BUILDER_MAX_FILE_BYTES) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill file exceeds the Builder byte limit",
        path: ["content"],
      });
    }
    if (contentBytes !== value.size_bytes) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill file size does not match its UTF-8 content",
        path: ["size_bytes"],
      });
    }
  });

const skillBuilderFilesSchema = z
  .array(skillBuilderFileSchema)
  .max(SKILL_BUILDER_MAX_FILES)
  .superRefine((files, context) => {
    if (
      files.reduce((total, file) => total + file.size_bytes, 0) >
      SKILL_BUILDER_MAX_TOTAL_BYTES
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill candidate package exceeds the Builder byte limit",
      });
    }
  });

/** Pinned base-version file identity (metadata only) for revision diffs. */
export const skillBuilderBaseFileSchema = z
  .object({
    path: skillBuilderFilePathSchema,
    media_type: z.string().trim().min(1).max(255),
    size_bytes: z.number().int().nonnegative(),
    sha256: z.string().regex(/^[a-f0-9]{64}$/u),
  })
  .strict();

export const skillBuilderSecretRequirementSchema = z
  .object({
    name: z.string().trim().min(1),
    target_env: z.string().trim().min(1),
    optional: z.boolean(),
  })
  .strict();

export const skillBuilderValidationSchema = z
  .object({
    draft_checksum: checksumSchema,
    validated_at: timestampSchema,
    description: z.string(),
    frontmatter: z.record(z.unknown()),
    compatibility: z.string().nullable(),
    secret_requirements: z.array(skillBuilderSecretRequirementSchema),
    scan_decision: z.enum(["allow", "warn"]),
    scan_rule_ids: z.array(z.string().trim().min(1)),
    scan_summary: z.record(z.unknown()),
  })
  .strict();

export const skillBuilderSkillDependencySchema = z
  .object({
    kind: z.literal("skill"),
    reference: z.string().trim().min(1).max(512),
    scope: z.enum(["project", "system"]),
    skill_id: uuidSchema,
    version_id: uuidSchema,
    version_number: z.number().int().positive(),
    slug: z.string().trim().min(1).max(63),
    display_name: z.string().trim().min(1).max(120),
    payload_checksum: checksumSchema,
    authoring_only: z.literal(true),
    runtime_authorized: z.literal(false),
  })
  .strict();

export const skillBuilderMcpToolDependencySchema = z
  .object({
    kind: z.literal("mcp_tool"),
    reference: z.string().trim().min(1).max(512),
    scope: z.enum(["project", "system"]),
    mcp_server_id: uuidSchema,
    version_id: uuidSchema,
    version_number: z.number().int().positive(),
    server_slug: z.string().trim().min(1).max(63),
    server_name: z.string().trim().min(1).max(120),
    tool_name: z
      .string()
      .regex(/^[A-Za-z0-9_-]+$/u)
      .max(255),
    payload_checksum: checksumSchema,
    inventory_status: z.enum(["ready", "degraded"]),
    inventory_error_code: z
      .enum(["mcp_discovery_unavailable", "mcp_catalog_invalid"])
      .nullable(),
    last_success_at: timestampSchema,
    authoring_only: z.literal(true),
    runtime_authorized: z.literal(false),
  })
  .strict();

export const skillBuilderDependencySchema = z.discriminatedUnion("kind", [
  skillBuilderSkillDependencySchema,
  skillBuilderMcpToolDependencySchema,
]);

export const skillBuilderDependencySnapshotSchema = z
  .object({
    version: z.literal(1),
    draft_checksum: checksumSchema,
    requirements: z
      .array(skillBuilderDependencySchema)
      .max(64)
      .superRefine((requirements, context) => {
        const references = requirements.map((item) => item.reference);
        if (new Set(references).size !== references.length) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Skill Builder dependencies must be unique",
          });
        }
      }),
  })
  .strict()
  .superRefine((snapshot, context) => {
    snapshot.requirements.forEach((item, index) => {
      if (
        item.kind === "mcp_tool" &&
        (item.inventory_status === "ready") !==
          (item.inventory_error_code === null)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "MCP inventory status is inconsistent",
          path: ["requirements", index, "inventory_error_code"],
        });
      }
    });
  });

export const skillBuilderSessionSchema = z
  .object({
    id: uuidSchema,
    project_id: uuidSchema,
    owner_user_id: z.string().trim().min(1),
    thread_id: uuidSchema,
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
    status: skillBuilderStatusSchema,
    revision: z.number().int().positive(),
    messages: z.array(skillBuilderMessageSchema),
    active_clarification: skillBuilderClarificationRequestSchema.nullable(),
    progress: z.array(skillBuilderProgressItemSchema),
    files: skillBuilderFilesSchema,
    draft_checksum: checksumSchema.nullable(),
    validation: skillBuilderValidationSchema.nullable(),
    error_code: z.string().trim().min(1).nullable(),
    error_message: z.string().trim().min(1).nullable(),
    created_skill_id: uuidSchema.nullable(),
    // Rolling-compatible durable commit identity. Current Gateways always
    // return it; older responses may omit it while the commit payload still
    // carries the exact version.
    created_skill_version_id: uuidSchema.nullish(),
    authoring_dependencies: skillBuilderDependencySnapshotSchema.nullish(),
    session_kind: skillBuilderSessionKindSchema,
    target_skill_id: uuidSchema.nullable(),
    base_version_id: uuidSchema.nullable(),
    base_version_number: z.number().int().positive().nullable(),
    base_payload_checksum: checksumSchema.nullable(),
    target_skill_deleted: z.boolean(),
    base_files: z
      .array(skillBuilderBaseFileSchema)
      .max(SKILL_BUILDER_MAX_FILES),
    // Rolling-compatible durable Builder extension. Legacy Gateway responses
    // omit this property; an active asynchronous Run supplies it.
    activeRun: skillBuilderRunAdmissionSchema.nullish(),
    execution_preference: skillBuilderExecutionPreferenceSchema.nullish(),
    created_at: timestampSchema,
    updated_at: timestampSchema,
  })
  .strict();

export const skillBuilderSessionSummarySchema = z
  .object({
    id: uuidSchema,
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
    status: skillBuilderStatusSchema,
    revision: z.number().int().positive(),
    updated_at: timestampSchema,
    session_kind: skillBuilderSessionKindSchema,
  })
  .strict();

export const skillBuilderSessionResponseSchema = z
  .object({
    data: skillBuilderSessionSchema,
    request_id: z.string().trim().min(1),
  })
  .strict();

/** A turn is either the legacy synchronous session response or Run admission. */
export const skillBuilderTurnResponseSchema = z.union([
  skillBuilderSessionResponseSchema,
  skillBuilderRunAdmissionSchema,
]);

export const skillBuilderSessionListResponseSchema = z
  .object({
    data: z.array(skillBuilderSessionSummarySchema),
    request_id: z.string().trim().min(1),
  })
  .strict();

export const createSkillBuilderSessionInputSchema = z
  .object({
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1).max(120),
    idempotency_key: idempotencyKeySchema,
  })
  .strict();

/** Opens a Builder session seeded from an existing Skill's Current Version. */
export const createSkillBuilderRevisionInputSchema = z
  .object({
    kind: z.literal("revise"),
    skill_id: uuidSchema,
    idempotency_key: idempotencyKeySchema,
  })
  .strict();

// Mirrors the Gateway attachment rules: display-only name (no control chars
// or path separators) plus bounded UTF-8 text content.
export const skillBuilderAttachmentSchema = z
  .object({
    name: z
      .string()
      .trim()
      .min(1)
      .max(120)
      .refine(
        (name) =>
          !name.startsWith(".") && !/[\u0000-\u001f/\\:*?"<>|]/u.test(name),
        "附件名不能包含路径分隔符或控制字符",
      ),
    content: z
      .string()
      .refine(
        (content) =>
          !content.includes("\0") &&
          utf8ByteLength(content) <= SKILL_BUILDER_MAX_ATTACHMENT_BYTES,
        "附件内容超出大小限制",
      ),
  })
  .strict();

const skillBuilderAttachmentsSchema = z
  .array(skillBuilderAttachmentSchema)
  .max(SKILL_BUILDER_MAX_ATTACHMENTS)
  .superRefine((attachments, context) => {
    if (
      new Set(attachments.map((item) => item.name)).size !== attachments.length
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "附件名不能重复",
      });
    }
    if (
      attachments.reduce(
        (total, item) => total + utf8ByteLength(item.content),
        0,
      ) > SKILL_BUILDER_MAX_ATTACHMENTS_TOTAL_BYTES
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "附件总大小超出限制",
      });
    }
  });

export const skillBuilderMessageTurnSchema = z
  .object({
    kind: z.literal("message"),
    message: z.string().trim().min(1).max(SKILL_BUILDER_MAX_MESSAGE_CHARS),
    model_name: skillBuilderExecutionModelNameSchema.optional(),
    mode: z.enum(["flash", "thinking", "pro", "ultra"]).optional(),
    thinking_enabled: z.boolean().optional(),
    reasoning_effort: skillBuilderReasoningEffortSchema.nullable().optional(),
    attachments: skillBuilderAttachmentsSchema.optional(),
  })
  .strict();

export const skillBuilderClarificationResponseSchema = z
  .object({
    version: z.literal(1),
    kind: z.literal("human_input_response"),
    source: z.string().trim().min(1),
    request_id: z.string().trim().min(1),
    response_kind: z.enum(["option", "text"]),
    option_id: z.string().trim().min(1).optional(),
    value: z.string().trim().min(1).max(SKILL_BUILDER_MAX_MESSAGE_CHARS),
  })
  .strict();

export const skillBuilderClarificationTurnSchema = z
  .object({
    kind: z.literal("clarification"),
    response: skillBuilderClarificationResponseSchema,
    model_name: skillBuilderExecutionModelNameSchema.optional(),
    mode: z.enum(["flash", "thinking", "pro", "ultra"]).optional(),
    thinking_enabled: z.boolean().optional(),
    reasoning_effort: skillBuilderReasoningEffortSchema.nullable().optional(),
  })
  .strict();

export const skillBuilderFileChangeSchema = z
  .object({
    op: z.enum(["create", "replace", "delete"]),
    path: skillBuilderFilePathSchema,
    content: z.string().optional(),
    media_type: z.string().trim().min(1).max(255).optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.op !== "delete" &&
      (value.content === undefined || value.media_type === undefined)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Created and replaced files require content and media_type",
      });
    }
    if (
      value.op === "delete" &&
      (value.content !== undefined || value.media_type !== undefined)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Deleted files accept only op and path",
      });
    }
    if (
      value.content !== undefined &&
      utf8ByteLength(value.content) > SKILL_BUILDER_MAX_FILE_BYTES
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Skill file exceeds the Builder byte limit",
        path: ["content"],
      });
    }
  });

export const skillBuilderDraftTurnSchema = z
  .object({
    kind: z.literal("draft_update"),
    expected_draft_checksum: checksumSchema,
    changes: z
      .array(skillBuilderFileChangeSchema)
      .min(1)
      .max(SKILL_BUILDER_MAX_FILES)
      .superRefine((changes, context) => {
        if (
          changes.reduce(
            (total, change) =>
              total +
              (change.content === undefined
                ? 0
                : utf8ByteLength(change.content)),
            0,
          ) > SKILL_BUILDER_MAX_TOTAL_BYTES
        ) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Skill draft changes exceed the Builder byte limit",
          });
        }
      }),
  })
  .strict();

export const skillBuilderTurnInputSchema = z
  .object({
    input: z.discriminatedUnion("kind", [
      skillBuilderMessageTurnSchema,
      skillBuilderClarificationTurnSchema,
      skillBuilderDraftTurnSchema,
    ]),
    expected_revision: z.number().int().positive(),
    idempotency_key: idempotencyKeySchema,
  })
  .strict();

export const validateSkillBuilderSessionInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    expected_draft_checksum: checksumSchema,
    idempotency_key: idempotencyKeySchema,
  })
  .strict();

export const commitSkillBuilderSessionInputSchema =
  validateSkillBuilderSessionInputSchema
    .extend({
      acknowledge_warnings: z.boolean(),
    })
    .strict();

export const cancelSkillBuilderSessionInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    idempotency_key: idempotencyKeySchema,
  })
  .strict();

export const setSkillBuilderExecutionPreferenceInputSchema =
  skillBuilderExecutionPreferenceSchema;

export const skillBuilderCommitResponseSchema = z
  .object({
    data: z
      .object({
        session: skillBuilderSessionSchema,
        skill: currentVersionAssetSummarySchema,
        // Current Gateway responses return the exact committed version for
        // both create and revise. Nullable remains rolling-compatible with an
        // older Gateway that omitted the create version.
        version: skillVersionSchema.nullable(),
      })
      .strict(),
    request_id: z.string().trim().min(1),
  })
  .strict();

export type SkillBuilderStatus = z.infer<typeof skillBuilderStatusSchema>;
export type SkillBuilderRunStatus = z.infer<typeof skillBuilderRunStatusSchema>;
export type SkillBuilderRunAdmission = z.infer<
  typeof skillBuilderRunAdmissionSchema
>;
export type SkillBuilderRunToolStepProjection = z.infer<
  typeof skillBuilderRunToolStepProjectionSchema
>;
export type SkillBuilderRunStreamProjection = z.infer<
  typeof skillBuilderRunStreamProjectionSchema
>;
export type SkillBuilderReasoningEffort = z.infer<
  typeof skillBuilderReasoningEffortSchema
>;
export type SkillBuilderExecutionPreference = z.infer<
  typeof skillBuilderExecutionPreferenceSchema
>;
export type SkillBuilderAttachment = z.infer<
  typeof skillBuilderAttachmentSchema
>;
export type SkillBuilderProgressItem = z.infer<
  typeof skillBuilderProgressItemSchema
>;
export type SkillBuilderMessage = z.infer<typeof skillBuilderMessageSchema>;
export type SkillBuilderActivity = z.infer<typeof skillBuilderActivitySchema>;
export type SkillBuilderFile = z.infer<typeof skillBuilderFileSchema>;
export type SkillBuilderFileChange = z.infer<
  typeof skillBuilderFileChangeSchema
>;
export type SkillBuilderValidation = z.infer<
  typeof skillBuilderValidationSchema
>;
export type SkillBuilderDependency = z.infer<
  typeof skillBuilderDependencySchema
>;
export type SkillBuilderDependencySnapshot = z.infer<
  typeof skillBuilderDependencySnapshotSchema
>;
export type SkillBuilderSession = z.infer<typeof skillBuilderSessionSchema>;
export type SkillBuilderSessionKind = z.infer<
  typeof skillBuilderSessionKindSchema
>;
export type SkillBuilderBaseFile = z.infer<typeof skillBuilderBaseFileSchema>;
export type SkillBuilderSessionSummary = z.infer<
  typeof skillBuilderSessionSummarySchema
>;
export type CreateSkillBuilderSessionInput = z.input<
  typeof createSkillBuilderSessionInputSchema
>;
export type CreateSkillBuilderRevisionInput = z.input<
  typeof createSkillBuilderRevisionInputSchema
>;
export type SkillBuilderTurnInput = z.input<typeof skillBuilderTurnInputSchema>;
export type SkillBuilderTurnResponse = z.infer<
  typeof skillBuilderTurnResponseSchema
>;
export type ValidateSkillBuilderSessionInput = z.input<
  typeof validateSkillBuilderSessionInputSchema
>;
export type CommitSkillBuilderSessionInput = z.input<
  typeof commitSkillBuilderSessionInputSchema
>;
export type SkillBuilderCommitResponse = z.infer<
  typeof skillBuilderCommitResponseSchema
>;
export type CancelSkillBuilderSessionInput = z.input<
  typeof cancelSkillBuilderSessionInputSchema
>;
export type SetSkillBuilderExecutionPreferenceInput = z.input<
  typeof setSkillBuilderExecutionPreferenceInputSchema
>;

export function isSkillBuilderRunAdmission(
  response: SkillBuilderTurnResponse,
): response is SkillBuilderRunAdmission {
  return "runId" in response;
}
