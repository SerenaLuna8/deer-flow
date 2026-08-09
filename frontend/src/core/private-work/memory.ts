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

export const MEMORY_DOCUMENT_MAX_LENGTH = 16_000;
export const MEMORY_DIFF_MAX_LENGTH = 64_000;
export const MEMORY_VERSION_PAGE_SIZE = 50;

const memoryDateTimeSchema = z.string().max(64).datetime({ offset: true });
const memoryVersionSchema = z.number().int().positive();

export const memoryDocumentSchema = z
  .object({
    content: z.string().max(MEMORY_DOCUMENT_MAX_LENGTH),
    version: z.number().int().nonnegative(),
    updatedAt: memoryDateTimeSchema.nullable(),
    pendingCount: z.number().int().nonnegative(),
    dreamRunning: z.boolean(),
    injectionStatus: z.enum(["ok", "skipped_over_budget"]),
  })
  .strict();

const memoryVersionSummaryBaseSchema = z
  .object({
    version: memoryVersionSchema,
    trigger: z.enum([
      "auto_dream",
      "manual_dream",
      "restore",
      "budget_rewrite",
    ]),
    historyCount: z.number().int().min(0).max(20).nullable(),
    changed: z.boolean(),
    needsReview: z.boolean(),
    createdAt: memoryDateTimeSchema,
  })
  .strict();

function validateMemoryVersionHistory(
  value: z.infer<typeof memoryVersionSummaryBaseSchema>,
  context: z.RefinementCtx,
) {
  const valid =
    value.trigger === "restore"
      ? value.historyCount === null
      : value.trigger === "budget_rewrite"
        ? value.historyCount === 0
        : value.historyCount !== null && value.historyCount >= 1;
  if (!valid) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["historyCount"],
      message: "Memory version history count does not match its trigger",
    });
  }
}

export const memoryVersionSummarySchema =
  memoryVersionSummaryBaseSchema.superRefine(validateMemoryVersionHistory);

export const memoryVersionsSchema = z
  .object({
    items: z.array(memoryVersionSummarySchema).max(100),
  })
  .strict();

export const memoryVersionDetailSchema = memoryVersionSummaryBaseSchema
  .extend({
    content: z.string().max(MEMORY_DOCUMENT_MAX_LENGTH),
    unifiedDiff: z.string().max(MEMORY_DIFF_MAX_LENGTH),
  })
  .strict()
  .superRefine(validateMemoryVersionHistory);

const admittedDreamDispositionSchema = z.enum(["queued", "already_running"]);

export const memoryDreamResultSchema = z.union([
  z
    .object({
      disposition: z.literal("nothing_pending"),
      jobId: z.null(),
      historyCount: z.literal(0),
    })
    .strict(),
  z
    .object({
      disposition: admittedDreamDispositionSchema,
      jobId: z.string().uuid(),
      historyCount: z.literal(0),
      admissionKind: z.literal("budget_rewrite"),
    })
    .strict(),
  z
    .object({
      disposition: admittedDreamDispositionSchema,
      jobId: z.string().uuid(),
      historyCount: z.number().int().min(1).max(20),
    })
    .strict(),
]);

export const MEMORY_EPISODE_PAGE_SIZE = 20;
export const MEMORY_EPISODE_SEARCH_LIMIT = 50;
export const MEMORY_EPISODE_QUERY_MAX_LENGTH = 200;

export const memoryEpisodeTagSchema = z.enum([
  "permanent",
  "durable",
  "ephemeral",
  "correction",
]);

export const memoryEpisodeSchema = z
  .object({
    id: z.string().uuid(),
    threadId: z.string().min(1).max(64),
    origin: z.enum(["snip", "tool"]),
    taggedText: z.string().min(1).max(1_000),
    occurredAt: memoryDateTimeSchema,
    createdAt: memoryDateTimeSchema,
  })
  .strict();

export const memoryEpisodesSchema = z
  .object({
    items: z.array(memoryEpisodeSchema).max(50),
  })
  .strict();

const memoryEpisodesInputSchema = z
  .object({
    q: z.string().min(1).max(MEMORY_EPISODE_QUERY_MAX_LENGTH).optional(),
    tags: z.array(memoryEpisodeTagSchema).max(4).optional(),
    before: memoryDateTimeSchema.optional(),
    limit: z.number().int().min(1).max(50),
  })
  .strict();

export const MEMORY_PENDING_PAGE_SIZE = 50;

export const memoryPendingEntrySchema = z
  .object({
    sequence: z.number().int().positive(),
    origin: z.enum(["snip", "tool"]),
    taggedText: z.string().min(1).max(1_000),
    createdAt: memoryDateTimeSchema,
  })
  .strict();

export const memoryPendingSchema = z
  .object({
    items: z.array(memoryPendingEntrySchema).max(100),
  })
  .strict();

export type MemoryDocument = z.infer<typeof memoryDocumentSchema>;
export type MemoryVersionSummary = z.infer<typeof memoryVersionSummarySchema>;
export type MemoryVersionDetail = z.infer<typeof memoryVersionDetailSchema>;
export type MemoryDreamResult = z.infer<typeof memoryDreamResultSchema>;
export type MemoryEpisode = z.infer<typeof memoryEpisodeSchema>;
export type MemoryEpisodeTag = z.infer<typeof memoryEpisodeTagSchema>;
export type MemoryEpisodesInput = z.infer<typeof memoryEpisodesInputSchema>;
export type MemoryPendingEntry = z.infer<typeof memoryPendingEntrySchema>;

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
    baseURL: `${access.apiBaseURL.slice(0, -privateSuffix.length)}/projects/${scope.projectId}/memory`,
  };
}

async function readJSON<T>(
  response: Response,
  schema: z.ZodType<T>,
  fallback: string,
) {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return schema.parse(await response.json());
}

function parseVersion(version: number) {
  return memoryVersionSchema.parse(version);
}

export function projectMemoryRootQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory");
}

export function projectMemoryDocumentQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory", "document");
}

export function projectMemoryVersionsQueryKey(
  scope: ProjectClientScope,
  input: { limit: number; offset: number },
) {
  const parameters = z
    .object({
      limit: z.number().int().min(1).max(100),
      offset: z.number().int().min(0).max(10_000),
    })
    .strict()
    .parse(input);
  return privateWorkQueryKey(
    scope,
    "memory",
    "versions",
    parameters.limit,
    parameters.offset,
  );
}

export function projectMemoryVersionQueryKey(
  scope: ProjectClientScope,
  version: number,
) {
  return privateWorkQueryKey(scope, "memory", "version", parseVersion(version));
}

const memoryEpisodesFilterSchema = z
  .object({
    q: z.string().min(1).max(MEMORY_EPISODE_QUERY_MAX_LENGTH).optional(),
    tags: z.array(memoryEpisodeTagSchema).max(4).optional(),
  })
  .strict();

export type MemoryEpisodesFilter = z.infer<typeof memoryEpisodesFilterSchema>;

export function projectMemoryEpisodesQueryKey(
  scope: ProjectClientScope,
  input: MemoryEpisodesFilter,
) {
  const parameters = memoryEpisodesFilterSchema.parse(input);
  return privateWorkQueryKey(
    scope,
    "memory",
    "episodes",
    parameters.q ?? null,
    [...(parameters.tags ?? [])].sort().join(","),
  );
}

export function projectMemoryPendingQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory", "pending");
}

export function projectMemoryMutationKey(
  scope: ProjectClientScope,
  action: "dream" | "restore",
) {
  return privateWorkQueryKey(scope, "memory", "mutation", action);
}

export function projectMemoryPermissions(capabilities: readonly Capability[]) {
  const canRead = capabilities.includes("private_work.read_own");
  const canCreate = capabilities.includes("private_work.create");
  return {
    canRead,
    canDream: canCreate,
    canRestore: canCreate,
  };
}

export async function getProjectMemory(
  access: ProjectMemoryAccess,
  signal?: AbortSignal,
) {
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(baseURL, { signal });
  return readJSON(
    response,
    memoryDocumentSchema,
    "Failed to load project Memory",
  );
}

export async function dreamProjectMemory(
  access: ProjectMemoryAccess,
  input: { threadId?: string } = {},
  signal?: AbortSignal,
) {
  const body = z
    .object({ threadId: z.string().min(1).max(64).optional() })
    .strict()
    .parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(`${baseURL}/dream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return readJSON(
    response,
    memoryDreamResultSchema,
    "Failed to organize project Memory",
  );
}

export async function listProjectMemoryEpisodes(
  access: ProjectMemoryAccess,
  input: MemoryEpisodesInput,
  signal?: AbortSignal,
) {
  const parameters = memoryEpisodesInputSchema.parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const search = new URLSearchParams();
  if (parameters.q !== undefined) search.set("q", parameters.q);
  for (const tag of parameters.tags ?? []) search.append("tags", tag);
  if (parameters.before !== undefined) search.set("before", parameters.before);
  search.set("limit", String(parameters.limit));
  const response = await fetchWithAuth(`${baseURL}/episodes?${search}`, {
    signal,
  });
  return readJSON(
    response,
    memoryEpisodesSchema,
    "Failed to load project Memory archive",
  );
}

export async function listProjectMemoryPending(
  access: ProjectMemoryAccess,
  input: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) {
  const parameters = z
    .object({
      limit: z.number().int().min(1).max(100).default(MEMORY_PENDING_PAGE_SIZE),
      offset: z.number().int().min(0).max(10_000).default(0),
    })
    .strict()
    .parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const search = new URLSearchParams({
    limit: String(parameters.limit),
    offset: String(parameters.offset),
  });
  const response = await fetchWithAuth(`${baseURL}/pending?${search}`, {
    signal,
  });
  return readJSON(
    response,
    memoryPendingSchema,
    "Failed to load project Memory backlog",
  );
}

export async function listProjectMemoryVersions(
  access: ProjectMemoryAccess,
  input: { limit: number; offset: number },
  signal?: AbortSignal,
) {
  const parameters = z
    .object({
      limit: z.number().int().min(1).max(100),
      offset: z.number().int().min(0).max(10_000),
    })
    .strict()
    .parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const search = new URLSearchParams({
    limit: String(parameters.limit),
    offset: String(parameters.offset),
  });
  const response = await fetchWithAuth(`${baseURL}/versions?${search}`, {
    signal,
  });
  return readJSON(
    response,
    memoryVersionsSchema,
    "Failed to load project Memory versions",
  );
}

export async function getProjectMemoryVersion(
  access: ProjectMemoryAccess,
  version: number,
  signal?: AbortSignal,
) {
  const parsedVersion = parseVersion(version);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(`${baseURL}/versions/${parsedVersion}`, {
    signal,
  });
  return readJSON(
    response,
    memoryVersionDetailSchema,
    "Failed to load project Memory version",
  );
}

export async function restoreProjectMemoryVersion(
  access: ProjectMemoryAccess,
  version: number,
  input: { expectedCurrentVersion: number },
  signal?: AbortSignal,
) {
  const parsedVersion = parseVersion(version);
  const body = z
    .object({
      expectedCurrentVersion: z.number().int().nonnegative(),
    })
    .strict()
    .parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    `${baseURL}/versions/${parsedVersion}/restore`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return readJSON(
    response,
    memoryVersionDetailSchema,
    "Failed to restore project Memory version",
  );
}
