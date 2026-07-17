"use client";

import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  projectClientScopeSchema,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "@/core/private-work/types";

import {
  projectGovernanceBaseURL,
  readProjectGovernanceResponse,
  requestProjectGovernance,
} from "./usage";

const PRIVATE_METADATA_KEYS = new Set([
  "attempt_id",
  "ciphertext",
  "job_id",
  "nonce",
  "owner_user_id",
  "project_id",
  "request_id",
  "secret",
  "target_ref_hmac",
  "target_ref_key_id",
]);

function containsPrivateKey(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(containsPrivateKey);
  if (typeof value !== "object" || value === null) return false;
  return Object.entries(value).some(
    ([key, item]) =>
      PRIVATE_METADATA_KEYS.has(key.toLowerCase()) || containsPrivateKey(item),
  );
}

const publicMetadataSchema = z
  .record(z.string(), z.unknown())
  .superRefine((value, context) => {
    if (containsPrivateKey(value)) {
      context.addIssue({
        code: "custom",
        message: "Private audit metadata is forbidden",
      });
    }
  });

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
      "recovery",
      "system_admin",
    ]),
    action: z.string().min(1).max(64),
    target_kind: z.enum([
      "project",
      "invitation",
      "membership",
      "asset",
      "automation",
      "quota",
      "run",
      "job",
      "backup",
      "restore",
      "purge",
      "audit",
    ]),
    outcome: z.enum(["success", "rejected", "failed"]),
    public_error_code: z
      .string()
      .regex(/^[A-Z][A-Z0-9_]{0,63}$/u)
      .nullable(),
    metadata: publicMetadataSchema,
  })
  .strict();

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
  const parsed = projectClientScopeSchema.parse(scope);
  return [
    "account",
    parsed.accountId,
    "project",
    parsed.projectId,
    "governance",
    "audit",
    cursor,
    limit,
  ] as const;
}

function requiredScope(access: PrivateWorkAccess): ProjectClientScope {
  if (!access.scope) throw new Error("Project governance scope is unavailable");
  return access.scope;
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
  access: PrivateWorkAccess,
  cursor: string | null = null,
  limit = 50,
  enabled = true,
) {
  const scope = access.scope;
  return {
    queryKey: scope
      ? projectAuditQueryKey(scope, cursor, limit)
      : (["governance", "audit", "inactive"] as const),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchProjectAudit(requiredScope(access), cursor, limit, signal),
    enabled: enabled && scope !== null,
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function useProjectAudit(
  cursor: string | null = null,
  limit = 50,
  enabled = true,
) {
  const access = usePrivateWorkAccess();
  return useQuery(projectAuditQueryOptions(access, cursor, limit, enabled));
}

export type ProjectAuditItem = z.infer<typeof auditItemSchema>;
export type ProjectAuditPage = z.infer<typeof auditPageSchema>;
