"use client";

import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

import { governanceRoot } from "./query-keys";
import {
  projectGovernanceBaseURL,
  readProjectGovernanceResponse,
  requestProjectGovernance,
} from "./usage";

export const auditActionSchema = z.enum([
  "project.created",
  "project.updated",
  "project.suspended",
  "project.resumed",
  "project.deletion_requested",
  "project.recovered",
  "invitation.created",
  "invitation.revoked",
  "invitation.redeemed",
  "member.joined",
  "member.role_changed",
  "member.removed",
  "member.left",
  "asset.created",
  "asset.updated",
  "asset.published",
  "asset.deprecated",
  "asset.deleted",
  "asset.bound",
  "asset.unbound",
  "asset.credential_created",
  "asset.credential_replaced",
  "asset.credential_revoked",
  "asset.credential_deleted",
  "asset.credential_grants_migrated",
  "automation.created",
  "automation.updated",
  "automation.deleted",
  "automation.triggered",
  "quota.policy_updated",
  "quota.reconciled",
  "run.admitted",
  "run.cancel_requested",
  "run.files_finalized",
  "run.terminal",
  "memory.remember",
  "memory.recall.executed",
  "memory.seal.admitted",
  "memory.seal.settled",
  "memory.dream.admitted",
  "memory.dream.settled",
  "memory.injection.skipped",
  "memory.dream.review_flagged",
  "memory.restore.executed",
  "memory.reset.executed",
  "job.dead",
  "job.requeued",
  "purge.completed",
  "audit.corrected",
  "system_setting.updated",
]);

type AuditAction = z.infer<typeof auditActionSchema>;

const emptyMetadataSchema = z.object({}).strict();
const roleSchema = z.enum(["admin", "editor", "runner", "viewer"]);
const roleMetadataSchema = z.object({ role: roleSchema }).strict();
const roleChangedMetadataSchema = z
  .object({ previous_role: roleSchema, role: roleSchema })
  .strict();
const assetOperationSchema = z.enum([
  "agent.create",
  "agent.version.create",
  "agent.instructions.update",
  "agent.capability_bindings.update",
  "agent.version.restore",
  "agent.publish",
  "agent.delete",
  "agent.activate",
  "agent.suspend",
  "agent.default.set",
  "agent.default.clear",
  "skill.create",
  "skill.version.create",
  "skill.publish",
  "skill.version.revoke",
  "skill.delete",
  "skill.activate",
  "skill.credential_bindings.configure",
  "skill.suspend",
  "mcp.create",
  "mcp.version.create",
  "mcp.submit_approval",
  "mcp.approve",
  "mcp.credential_grants.configure",
  "mcp.publish",
  "mcp.archive",
  "mcp.suspend",
  "mcp.activate",
  "mcp.delete",
  "credential.create",
  "credential.replace",
  "credential.revoke",
  "credential.delete",
  "credential.grants.migrate",
  "binding.enable",
  "binding.upgrade",
  "binding.rollback",
  "binding.sync_current",
  "binding.disable",
]);
const versionedAgentOperations = new Set([
  "agent.version.create",
  "agent.instructions.update",
  "agent.capability_bindings.update",
  "agent.version.restore",
  "agent.publish",
  "agent.activate",
]);
const versionedSkillOperations = new Set(["skill.version.revoke"]);
const currentAssetMetadataSchema = z
  .object({
    asset_kind: z.enum(["agent", "skill", "mcp"]),
    operation: assetOperationSchema,
    version_number: z.number().int().positive().optional(),
  })
  .strict()
  .superRefine((value, context) => {
    const [operationDomain = ""] = value.operation.split(".", 1);
    const kindMatches =
      !["agent", "skill", "mcp"].includes(operationDomain) ||
      operationDomain === value.asset_kind;
    const credentialMatches =
      operationDomain !== "credential" || value.asset_kind === "mcp";
    const versionMatches =
      operationDomain === "agent"
        ? versionedAgentOperations.has(value.operation) ===
          (value.version_number !== undefined)
        : operationDomain === "skill"
          ? versionedSkillOperations.has(value.operation) ===
            (value.version_number !== undefined)
          : value.version_number === undefined;
    if (!kindMatches || !credentialMatches || !versionMatches) {
      context.addIssue({
        code: "custom",
        message: "Asset audit metadata is inconsistent",
      });
    }
  });
const legacyAssetMetadataSchema = z
  .object({ asset_kind: z.enum(["agent", "skill", "mcp"]) })
  .strict();
const assetMetadataSchema = z.union([
  currentAssetMetadataSchema,
  legacyAssetMetadataSchema,
]);
const automationMetadataSchema = z
  .object({ trigger_kind: z.enum(["manual", "scheduled"]).optional() })
  .strict();
const automationTriggeredMetadataSchema = z
  .object({ trigger_kind: z.enum(["manual", "scheduled"]) })
  .strict();
const publicErrorCodeSchema = z.string().regex(/^[A-Z][A-Z0-9_]{0,63}$/u);
const quotaPolicyMetadataSchema = z
  .object({
    member_limit: z.number().int().min(1).nullable().optional(),
    storage_bytes_limit: z.number().int().nonnegative().nullable().optional(),
    concurrent_run_limit: z.number().int().min(1).nullable().optional(),
    mcp_calls_daily_limit: z.number().int().nonnegative().nullable().optional(),
    version: z.number().int().min(1),
  })
  .strict();
const quotaReconciledMetadataSchema = z
  .object({ changed_dimensions: z.number().int().min(0).max(4) })
  .strict();
const runAdmittedMetadataSchema = z
  .object({
    job_type: z.enum(["private_run", "automation_run"]),
    non_interactive: z.boolean(),
  })
  .strict();
const runTerminalMetadataSchema = z
  .object({
    job_type: z.enum(["private_run", "automation_run"]),
    status: z.enum(["completed", "failed", "cancelled"]),
    public_error_code: publicErrorCodeSchema.nullable().optional(),
  })
  .strict();
const runFilesFinalizedMetadataSchema = z
  .object({
    created_count: z.number().int().nonnegative(),
    modified_count: z.number().int().nonnegative(),
    deleted_count: z.number().int().nonnegative(),
    artifact_count: z.number().int().nonnegative(),
    committed_bytes: z.number().int().nonnegative(),
  })
  .strict();
const memoryRememberMetadataSchema = z
  .object({
    kind: z.enum(["permanent", "durable", "ephemeral", "correction"]),
  })
  .strict();
const memoryRecallExecutedMetadataSchema = z
  .object({
    result_bucket: z.enum(["0", "1-2", "3+"]),
    matched_stage: z.enum(["exact", "similarity", "none"]),
    tags_filtered: z.boolean(),
    query_len_bucket: z.enum(["1-4", "5-16", "17-64", "65-200"]),
  })
  .strict();
const memorySealSettledMetadataSchema = z
  .object({
    disposition: z.enum(["sealed", "noop"]),
  })
  .strict();
const memoryDreamAdmittedMetadataSchema = z
  .object({
    origin: z.enum(["manual", "scheduled", "prepared"]),
    trigger: z.enum(["auto_dream", "manual_dream", "budget_rewrite"]),
    history_count: z.number().int().min(0).max(20),
  })
  .strict()
  .superRefine((value, context) => {
    const originMatches =
      (value.origin === "manual" &&
        ["manual_dream", "budget_rewrite"].includes(value.trigger)) ||
      (value.origin === "scheduled" &&
        ["auto_dream", "budget_rewrite"].includes(value.trigger)) ||
      (value.origin === "prepared" &&
        ["manual_dream", "budget_rewrite"].includes(value.trigger));
    const historyMatches =
      value.trigger === "budget_rewrite"
        ? value.history_count === 0
        : value.history_count >= 1;
    if (!originMatches || !historyMatches) {
      context.addIssue({
        code: "custom",
        message: "Memory Dream admission metadata is inconsistent",
      });
    }
  });
const memoryDreamSettledMetadataSchema = z
  .object({
    disposition: z.enum(["published", "cancelled", "dead"]),
    version: z.number().int().min(1).optional(),
    public_error_code: publicErrorCodeSchema.optional(),
  })
  .strict()
  .superRefine((value, context) => {
    const valid =
      (value.disposition === "published" &&
        value.version !== undefined &&
        value.public_error_code === undefined) ||
      (value.disposition === "cancelled" &&
        value.version === undefined &&
        value.public_error_code === undefined) ||
      (value.disposition === "dead" &&
        value.version === undefined &&
        value.public_error_code !== undefined);
    if (!valid) {
      context.addIssue({
        code: "custom",
        message: "Memory Dream settlement metadata is inconsistent",
      });
    }
  });
const memoryRestoreMetadataSchema = z
  .object({
    source_version: z.number().int().min(1),
    previous_version: z.number().int().min(1),
    published_version: z.number().int().min(2),
    changed: z.boolean(),
  })
  .strict()
  .refine(
    (value) => value.published_version === value.previous_version + 1,
    "Memory restore versions are inconsistent",
  );
const memoryResetCountFields = {
  projects_affected: z.number().int().nonnegative().optional(),
  scopes_reset: z.number().int().nonnegative().optional(),
  history_entries: z.number().int().nonnegative().optional(),
  documents: z.number().int().nonnegative().optional(),
  versions: z.number().int().nonnegative().optional(),
  dream_runs: z.number().int().nonnegative().optional(),
  prepare_runs: z.number().int().nonnegative().optional(),
  snapshots: z.number().int().nonnegative().optional(),
  episodes: z.number().int().nonnegative().optional(),
  jobs_cancelled: z.number().int().nonnegative().optional(),
};
const memoryResetMetadataSchema = z
  .object({
    scope: z.enum(["account", "project"]),
    ...memoryResetCountFields,
  })
  .strict()
  .superRefine((value, context) => {
    const counts = Object.keys(memoryResetCountFields).map(
      (key) => value[key as keyof typeof memoryResetCountFields],
    );
    const valid =
      value.scope === "account"
        ? counts.every((entry) => entry !== undefined)
        : counts.every((entry) => entry === undefined);
    if (!valid) {
      context.addIssue({
        code: "custom",
        message: "Memory reset metadata is inconsistent",
      });
    }
  });
const memoryInjectionSkippedMetadataSchema = z
  .object({
    reason: z.enum(["over_budget"]),
  })
  .strict();
const memoryDreamReviewFlaggedMetadataSchema = z
  .object({
    version: z.number().int().min(1),
    deletion_ratio_bucket: z.enum([
      "40-50%",
      "50-60%",
      "60-70%",
      "70-80%",
      "80-90%",
      "90-100%",
    ]),
  })
  .strict();
const jobMetadataSchema = z
  .object({
    job_type: z.enum([
      "private_run",
      "automation_run",
      "retention_purge",
      "mcp_discovery",
      "memory_dream",
      "memory_dream_prepare",
      "memory_seal",
    ]),
    public_error_code: publicErrorCodeSchema.nullable().optional(),
    attempt_count: z.number().int().min(0).max(20),
    retry_safety: z.enum(["safe", "unknown", "unsafe"]),
  })
  .strict();
const purgeMetadataSchema = z
  .object({
    resource_kind: z.enum(["project", "account", "file", "former_owner"]),
    purged_count: z.number().int().nonnegative(),
  })
  .strict();
const correctionMetadataSchema = z
  .object({ correction_kind: z.enum(["outcome", "metadata", "target"]) })
  .strict();
const systemSettingMetadataSchema = z
  .object({
    section: z.enum([
      "agent_runtime",
      "auth",
      "automations",
      "memory_document",
      "quotas",
    ]),
    revision: z.number().int().min(2),
    schema_version: z.number().int().min(1),
    payload_checksum: z.string().regex(/^[0-9a-f]{64}$/u),
    effect_scope: z.enum([
      "new_requests_and_runs",
      "new_requests",
      "new_memory_documents",
      "next_authoritative_check",
    ]),
  })
  .strict();

const auditMetadataSchemas: Record<AuditAction, z.ZodTypeAny> = {
  "project.created": emptyMetadataSchema,
  "project.updated": emptyMetadataSchema,
  "project.suspended": emptyMetadataSchema,
  "project.resumed": emptyMetadataSchema,
  "project.deletion_requested": emptyMetadataSchema,
  "project.recovered": emptyMetadataSchema,
  "invitation.created": roleMetadataSchema,
  "invitation.revoked": emptyMetadataSchema,
  "invitation.redeemed": roleMetadataSchema,
  "member.joined": roleMetadataSchema,
  "member.role_changed": roleChangedMetadataSchema,
  "member.removed": emptyMetadataSchema,
  "member.left": emptyMetadataSchema,
  "asset.created": assetMetadataSchema,
  "asset.updated": assetMetadataSchema,
  "asset.published": assetMetadataSchema,
  "asset.deprecated": assetMetadataSchema,
  "asset.deleted": assetMetadataSchema,
  "asset.bound": assetMetadataSchema,
  "asset.unbound": assetMetadataSchema,
  "asset.credential_created": assetMetadataSchema,
  "asset.credential_replaced": assetMetadataSchema,
  "asset.credential_revoked": assetMetadataSchema,
  "asset.credential_deleted": assetMetadataSchema,
  "asset.credential_grants_migrated": assetMetadataSchema,
  "automation.created": automationMetadataSchema,
  "automation.updated": automationMetadataSchema,
  "automation.deleted": automationMetadataSchema,
  "automation.triggered": automationTriggeredMetadataSchema,
  "quota.policy_updated": quotaPolicyMetadataSchema,
  "quota.reconciled": quotaReconciledMetadataSchema,
  "run.admitted": runAdmittedMetadataSchema,
  "run.cancel_requested": emptyMetadataSchema,
  "run.files_finalized": runFilesFinalizedMetadataSchema,
  "run.terminal": runTerminalMetadataSchema,
  "memory.remember": memoryRememberMetadataSchema,
  "memory.recall.executed": memoryRecallExecutedMetadataSchema,
  "memory.seal.admitted": emptyMetadataSchema,
  "memory.seal.settled": memorySealSettledMetadataSchema,
  "memory.dream.admitted": memoryDreamAdmittedMetadataSchema,
  "memory.dream.settled": memoryDreamSettledMetadataSchema,
  "memory.injection.skipped": memoryInjectionSkippedMetadataSchema,
  "memory.dream.review_flagged": memoryDreamReviewFlaggedMetadataSchema,
  "memory.restore.executed": memoryRestoreMetadataSchema,
  "memory.reset.executed": memoryResetMetadataSchema,
  "job.dead": jobMetadataSchema,
  "job.requeued": jobMetadataSchema,
  "purge.completed": purgeMetadataSchema,
  "audit.corrected": correctionMetadataSchema,
  "system_setting.updated": systemSettingMetadataSchema,
};

export const auditItemSchema = z
  .object({
    id: z.string().uuid(),
    occurred_at: z.string().datetime({ offset: true }),
    actor: z.enum([
      "user",
      "gateway",
      "worker",
      "scheduler",
      "operator",
      "migration",
      "system_admin",
    ]),
    action: auditActionSchema,
    target_kind: z.enum([
      "project",
      "invitation",
      "membership",
      "asset",
      "automation",
      "quota",
      "run",
      "job",
      "purge",
      "audit",
      "system_setting",
      "account",
    ]),
    outcome: z.enum(["success", "rejected", "failed"]),
    public_error_code: z
      .string()
      .regex(/^[A-Z][A-Z0-9_]{0,63}$/u)
      .nullable(),
    metadata: z.record(z.string(), z.unknown()),
  })
  .strict()
  .superRefine((item, context) => {
    const result = auditMetadataSchemas[item.action].safeParse(item.metadata);
    if (!result.success) {
      for (const issue of result.error.issues) {
        context.addIssue({
          code: "custom",
          path: ["metadata", ...issue.path],
          message: issue.message,
        });
      }
    }
  });

export const auditPageSchema = z
  .object({
    items: z.array(auditItemSchema),
    next_cursor: z.string().min(1).nullable(),
  })
  .strict();

export function projectAuditQueryKey(
  scope: ProjectClientScope,
  cursor: string | null = null,
  limit = 50,
) {
  return [...governanceRoot(scope), "audit", cursor, limit] as const;
}

export async function fetchProjectAudit(
  scope: ProjectClientScope,
  cursor: string | null = null,
  limit = 50,
  signal?: AbortSignal,
): Promise<ProjectAuditPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  const response = await requestProjectGovernance(
    `${projectGovernanceBaseURL(scope)}/audit?${params.toString()}`,
    { signal },
  );
  return readProjectGovernanceResponse(response, auditPageSchema);
}

export function projectAuditQueryOptions(
  scope: ProjectClientScope,
  cursor: string | null = null,
  limit = 50,
  enabled = true,
) {
  const parsed = projectClientScopeSchema.parse(scope);
  return {
    queryKey: projectAuditQueryKey(parsed, cursor, limit),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchProjectAudit(parsed, cursor, limit, signal),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function useProjectAudit(
  scope: ProjectClientScope,
  cursor: string | null = null,
  limit = 50,
) {
  return useQuery(projectAuditQueryOptions(scope, cursor, limit));
}

export type ProjectAuditItem = z.infer<typeof auditItemSchema>;
export type ProjectAuditPage = z.infer<typeof auditPageSchema>;
