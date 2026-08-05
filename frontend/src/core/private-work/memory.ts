import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import type { Capability } from "@/core/projects/types";

import { privateWorkQueryKey } from "./query-keys";
import {
  projectClientScopeSchema,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "./types";

export const MEMORY_V2_CATEGORY_MAX_LENGTH = 32;
export const MEMORY_V2_CONTENT_MAX_LENGTH = 16_000;

const memoryNamespaceSchema = z.string().min(1).max(128);
const memoryDateTimeSchema = z.string().max(64).datetime({ offset: true });
const memoryFactStatusSchema = z.enum(["active", "disabled"]);
const memoryCandidateStatusSchema = z.enum([
  "pending",
  "accepted",
  "rejected",
  "superseded",
]);

export const memoryV2RevisionSchema = z
  .object({
    id: z.string().uuid(),
    factId: z.string().uuid(),
    revisionNumber: z.number().int().positive(),
    revisionSequence: z.number().int().positive(),
    content: z.string().max(MEMORY_V2_CONTENT_MAX_LENGTH).nullable(),
    contentDigest: z.string().length(64),
    category: z.string().min(1).max(MEMORY_V2_CATEGORY_MAX_LENGTH),
    confidence: z.number().min(0).max(1),
    validFrom: memoryDateTimeSchema.nullable(),
    validTo: memoryDateTimeSchema.nullable(),
    lastConfirmedAt: memoryDateTimeSchema.nullable(),
    changedBy: z.enum(["user", "system", "consolidator"]),
    sourceCandidateId: z.string().uuid().nullable(),
    supersedesRevisionId: z.string().uuid().nullable(),
    changeReason: z.string().max(64).nullable(),
    contentErasedAt: memoryDateTimeSchema.nullable(),
    createdAt: memoryDateTimeSchema,
  })
  .strict();

export const memoryV2FactSchema = z
  .object({
    id: z.string().uuid(),
    factKind: z.string().min(1).max(32),
    status: memoryFactStatusSchema,
    version: z.number().int().positive(),
    disabledAt: memoryDateTimeSchema.nullable(),
    supersededAt: memoryDateTimeSchema.nullable(),
    deletedAt: memoryDateTimeSchema.nullable(),
    createdAt: memoryDateTimeSchema,
    updatedAt: memoryDateTimeSchema,
    currentRevision: memoryV2RevisionSchema,
  })
  .strict();

export const memoryV2CandidateSchema = z
  .object({
    id: z.string().uuid(),
    candidateType: z.string().min(1).max(32),
    content: z.string().max(MEMORY_V2_CONTENT_MAX_LENGTH).nullable(),
    confidence: z.number().min(0).max(1),
    retentionClass: z.enum(["permanent", "durable", "ephemeral"]),
    sensitivity: z.enum(["normal", "sensitive", "restricted"]),
    status: memoryCandidateStatusSchema,
    decisionReason: z.string().max(64).nullable(),
    decidedAt: memoryDateTimeSchema.nullable(),
    contentErasedAt: memoryDateTimeSchema.nullable(),
    createdAt: memoryDateTimeSchema,
    updatedAt: memoryDateTimeSchema,
  })
  .strict();

export const memoryV2EvidenceSchema = z
  .object({
    id: z.string().uuid(),
    factId: z.string().uuid(),
    revisionId: z.string().uuid(),
    sourceCandidateId: z.string().uuid().nullable(),
    sourceItemId: z.string().uuid().nullable(),
    threadId: z.string().max(64).nullable(),
    runId: z.string().max(64).nullable(),
    runEventSequence: z.number().int().nonnegative().nullable(),
    evidenceExcerpt: z.string().max(4_000).nullable(),
    trustClass: z.enum(["direct", "derived", "untrusted"]),
    sourceErasedAt: memoryDateTimeSchema.nullable(),
    createdAt: memoryDateTimeSchema,
  })
  .strict();

const memoryV2FactsResponseSchema = z
  .object({
    namespace: memoryNamespaceSchema,
    items: z.array(memoryV2FactSchema).max(100),
  })
  .strict();

const memoryV2CandidatesResponseSchema = z
  .object({
    namespace: memoryNamespaceSchema,
    items: z.array(memoryV2CandidateSchema).max(100),
  })
  .strict();

export const memoryV2FactDetailSchema = z
  .object({
    namespace: memoryNamespaceSchema,
    fact: memoryV2FactSchema,
    revisions: z.array(memoryV2RevisionSchema).max(1_000),
    evidence: z.array(memoryV2EvidenceSchema).max(5_000),
  })
  .strict();

export const memoryV2StatusSchema = z
  .object({
    enabled: z.boolean(),
    pipelineMode: z.enum(["off", "shadow", "consolidate", "v2"]),
    searchEnabled: z.boolean(),
    injectionEnabled: z.boolean(),
    consolidationIntervalMinutes: z.number().int().min(15).max(1_440),
    candidateRetentionDays: z.number().int().min(1).max(365),
  })
  .strict();

const memoryV2HardForgetResultSchema = z
  .object({
    factId: z.string().uuid(),
    version: z.number().int().min(2),
    status: z.literal("deleted"),
    erasedCandidates: z.number().int().nonnegative(),
    erasedRevisions: z.number().int().positive(),
    erasedEvidence: z.number().int().nonnegative(),
    erasedSourceItems: z.number().int().nonnegative(),
  })
  .strict();

export type MemoryV2Fact = z.infer<typeof memoryV2FactSchema>;
export type MemoryV2Revision = z.infer<typeof memoryV2RevisionSchema>;
export type MemoryV2Candidate = z.infer<typeof memoryV2CandidateSchema>;
export type MemoryV2Evidence = z.infer<typeof memoryV2EvidenceSchema>;
export type MemoryV2FactDetail = z.infer<typeof memoryV2FactDetailSchema>;
export type MemoryV2Status = z.infer<typeof memoryV2StatusSchema>;
export type MemoryV2HardForgetResult = z.infer<
  typeof memoryV2HardForgetResultSchema
>;
export type MemoryV2FactListStatus = "active" | "disabled" | "all";

type ProjectMemoryAccess = Pick<PrivateWorkAccess, "apiBaseURL" | "scope">;

function requireProjectMemoryAccess(access: ProjectMemoryAccess) {
  const scope = projectClientScopeSchema.parse(access.scope);
  const privateSuffix = `/projects/${scope.projectId}/private-work`;
  if (!access.apiBaseURL.endsWith(privateSuffix)) {
    throw new Error(
      "Project Memory requires a project-scoped private-work URL",
    );
  }
  return {
    scope,
    baseURL: `${access.apiBaseURL.slice(0, -privateSuffix.length)}/projects/${scope.projectId}/memory/v2`,
  };
}

function memoryV2URL(
  baseURL: string,
  path = "",
  parameters: Record<string, string | number | undefined> = {},
) {
  const search = new URLSearchParams({ namespace: "default" });
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== undefined) search.set(key, String(value));
  }
  return `${baseURL}${path}?${search.toString()}`;
}

async function readJSON<T>(
  response: Response,
  schema: z.ZodType<T>,
  fallback: string,
) {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return schema.parse(await response.json());
}

function pageParameters(input: {
  limit: number;
  offset: number;
  query?: string;
  category?: string;
}) {
  return z
    .object({
      limit: z.number().int().min(1).max(100),
      offset: z.number().int().nonnegative(),
      query: z.string().trim().min(1).max(200).optional(),
      category: z
        .string()
        .trim()
        .min(1)
        .max(MEMORY_V2_CATEGORY_MAX_LENGTH)
        .optional(),
    })
    .strict()
    .parse(input);
}

export function projectMemoryV2RootQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory", "v2", "default");
}

export function projectMemoryV2FactsQueryKey(
  scope: ProjectClientScope,
  input: {
    status: MemoryV2FactListStatus;
    limit: number;
    offset: number;
    query?: string;
    category?: string;
  },
) {
  return privateWorkQueryKey(
    scope,
    "memory",
    "v2",
    "default",
    "facts",
    input.status,
    input.limit,
    input.offset,
    input.query ?? "",
    input.category ?? "",
  );
}

export function projectMemoryV2CandidatesQueryKey(
  scope: ProjectClientScope,
  input: { limit: number; offset: number },
) {
  return privateWorkQueryKey(
    scope,
    "memory",
    "v2",
    "default",
    "candidates",
    "pending",
    input.limit,
    input.offset,
  );
}

export function projectMemoryV2StatusQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory", "v2", "default", "status");
}

export function projectMemoryV2FactDetailQueryKey(
  scope: ProjectClientScope,
  factId: string,
) {
  return privateWorkQueryKey(scope, "memory", "v2", "default", "fact", factId);
}

export function projectMemoryV2MutationKey(
  scope: ProjectClientScope,
  action:
    | "accept-candidate"
    | "reject-candidate"
    | "revise-fact"
    | "disable-fact"
    | "restore-fact"
    | "hard-forget"
    | "export",
) {
  return privateWorkQueryKey(
    scope,
    "memory",
    "v2",
    "default",
    "mutation",
    action,
  );
}

export function projectMemoryV2Permissions(
  capabilities: readonly Capability[],
) {
  const canRead = capabilities.includes("private_work.read_own");
  const canManage = capabilities.includes("private_work.create");
  return {
    canRead,
    canExport: canRead,
    canManage,
    canHardForget: canRead,
  };
}

export async function listProjectMemoryV2Facts(
  access: ProjectMemoryAccess,
  input: {
    status: MemoryV2FactListStatus;
    limit: number;
    offset: number;
    query?: string;
    category?: string;
  },
  signal?: AbortSignal,
) {
  const status = z.enum(["active", "disabled", "all"]).parse(input.status);
  const parameters = pageParameters({
    limit: input.limit,
    offset: input.offset,
    query: input.query,
    category: input.category,
  });
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, "/facts", { status, ...parameters }),
    { signal },
  );
  return readJSON(
    response,
    memoryV2FactsResponseSchema,
    "Failed to load project Memory facts",
  );
}

export async function listProjectMemoryV2Candidates(
  access: ProjectMemoryAccess,
  input: { limit: number; offset: number },
  signal?: AbortSignal,
) {
  const parameters = pageParameters(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, "/candidates", {
      status: "pending",
      ...parameters,
    }),
    { signal },
  );
  return readJSON(
    response,
    memoryV2CandidatesResponseSchema,
    "Failed to load project Memory candidates",
  );
}

export async function getProjectMemoryV2Fact(
  access: ProjectMemoryAccess,
  factId: string,
  signal?: AbortSignal,
) {
  const parsedFactId = z.string().uuid().parse(factId);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, `/facts/${encodeURIComponent(parsedFactId)}`),
    { signal },
  );
  return readJSON(
    response,
    memoryV2FactDetailSchema,
    "Failed to load project Memory history",
  );
}

export async function getProjectMemoryV2Status(
  access: ProjectMemoryAccess,
  signal?: AbortSignal,
) {
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(memoryV2URL(baseURL, "/status"), {
    signal,
  });
  return readJSON(
    response,
    memoryV2StatusSchema,
    "Failed to load project Memory settings",
  );
}

export async function acceptProjectMemoryV2Candidate(
  access: ProjectMemoryAccess,
  candidate: Pick<MemoryV2Candidate, "id" | "updatedAt">,
  signal?: AbortSignal,
) {
  const parsed = z
    .object({ id: z.string().uuid(), updatedAt: memoryDateTimeSchema })
    .strict()
    .parse({ id: candidate.id, updatedAt: candidate.updatedAt });
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, `/candidates/${encodeURIComponent(parsed.id)}/accept`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expectedUpdatedAt: parsed.updatedAt }),
      signal,
    },
  );
  return readJSON(
    response,
    memoryV2FactSchema,
    "Failed to accept project Memory candidate",
  );
}

export async function rejectProjectMemoryV2Candidate(
  access: ProjectMemoryAccess,
  candidate: Pick<MemoryV2Candidate, "id" | "updatedAt">,
  signal?: AbortSignal,
) {
  const parsed = z
    .object({ id: z.string().uuid(), updatedAt: memoryDateTimeSchema })
    .strict()
    .parse({ id: candidate.id, updatedAt: candidate.updatedAt });
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, `/candidates/${encodeURIComponent(parsed.id)}/reject`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expectedUpdatedAt: parsed.updatedAt }),
      signal,
    },
  );
  return readJSON(
    response,
    memoryV2CandidateSchema,
    "Failed to reject project Memory candidate",
  );
}

export async function reviseProjectMemoryV2Fact(
  access: ProjectMemoryAccess,
  fact: Pick<MemoryV2Fact, "id" | "version">,
  input: {
    content?: string;
    category?: string;
    confidence?: number;
    reason?: string;
  },
  signal?: AbortSignal,
) {
  const parsedFact = z
    .object({ id: z.string().uuid(), version: z.number().int().positive() })
    .strict()
    .parse({ id: fact.id, version: fact.version });
  const parsedInput = z
    .object({
      content: z
        .string()
        .trim()
        .min(1)
        .max(MEMORY_V2_CONTENT_MAX_LENGTH)
        .optional(),
      category: z
        .string()
        .trim()
        .min(1)
        .max(MEMORY_V2_CATEGORY_MAX_LENGTH)
        .optional(),
      confidence: z.number().min(0).max(1).optional(),
      reason: z.string().trim().min(1).max(64).optional(),
    })
    .strict()
    .refine(
      ({ content, category, confidence }) =>
        content !== undefined ||
        category !== undefined ||
        confidence !== undefined,
      "At least one Memory fact field must change",
    )
    .parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, `/facts/${encodeURIComponent(parsedFact.id)}`),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expectedVersion: parsedFact.version,
        ...parsedInput,
      }),
      signal,
    },
  );
  return readJSON(
    response,
    memoryV2FactSchema,
    "Failed to update project Memory fact",
  );
}

async function setProjectMemoryV2FactState(
  access: ProjectMemoryAccess,
  fact: Pick<MemoryV2Fact, "id" | "version">,
  action: "disable" | "restore",
  signal?: AbortSignal,
) {
  const parsed = z
    .object({ id: z.string().uuid(), version: z.number().int().positive() })
    .strict()
    .parse({ id: fact.id, version: fact.version });
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, `/facts/${encodeURIComponent(parsed.id)}/${action}`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expectedVersion: parsed.version }),
      signal,
    },
  );
  return readJSON(
    response,
    memoryV2FactSchema,
    `Failed to ${action} project Memory fact`,
  );
}

export function disableProjectMemoryV2Fact(
  access: ProjectMemoryAccess,
  fact: Pick<MemoryV2Fact, "id" | "version">,
  signal?: AbortSignal,
) {
  return setProjectMemoryV2FactState(access, fact, "disable", signal);
}

export function restoreProjectMemoryV2Fact(
  access: ProjectMemoryAccess,
  fact: Pick<MemoryV2Fact, "id" | "version">,
  signal?: AbortSignal,
) {
  return setProjectMemoryV2FactState(access, fact, "restore", signal);
}

export async function hardForgetProjectMemoryV2Fact(
  access: ProjectMemoryAccess,
  fact: Pick<MemoryV2Fact, "id" | "version">,
  signal?: AbortSignal,
) {
  const parsed = z
    .object({ id: z.string().uuid(), version: z.number().int().positive() })
    .strict()
    .parse({ id: fact.id, version: fact.version });
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    memoryV2URL(baseURL, `/facts/${encodeURIComponent(parsed.id)}/hard-forget`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expectedVersion: parsed.version }),
      signal,
    },
  );
  return readJSON(
    response,
    memoryV2HardForgetResultSchema,
    "Failed to permanently forget project Memory fact",
  );
}

export async function exportProjectMemoryV2(
  access: ProjectMemoryAccess,
  signal?: AbortSignal,
) {
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(memoryV2URL(baseURL, "/export"), {
    signal,
  });
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to export project Memory");
  }
  return response.blob();
}
