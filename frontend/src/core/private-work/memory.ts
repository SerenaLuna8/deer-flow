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
  })
  .strict();

export const memoryVersionSummarySchema = z
  .object({
    version: memoryVersionSchema,
    trigger: z.enum(["auto_dream", "manual_dream", "restore"]),
    historyCount: z.number().int().min(1).max(20).nullable(),
    changed: z.boolean(),
    createdAt: memoryDateTimeSchema,
  })
  .strict();

export const memoryVersionsSchema = z
  .object({
    items: z.array(memoryVersionSummarySchema).max(100),
  })
  .strict();

export const memoryVersionDetailSchema = memoryVersionSummarySchema
  .extend({
    content: z.string().max(MEMORY_DOCUMENT_MAX_LENGTH),
    unifiedDiff: z.string().max(MEMORY_DIFF_MAX_LENGTH),
  })
  .strict();

export const memoryDreamResultSchema = z
  .object({
    disposition: z.enum(["queued", "already_running", "nothing_pending"]),
    jobId: z.string().uuid().nullable(),
    historyCount: z.number().int().min(0).max(20),
  })
  .strict()
  .superRefine((value, context) => {
    const nothingPending = value.disposition === "nothing_pending";
    if (nothingPending !== (value.jobId === null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["jobId"],
        message: "Dream job identity does not match its disposition",
      });
    }
    if (nothingPending !== (value.historyCount === 0)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["historyCount"],
        message: "Dream history count does not match its disposition",
      });
    }
  });

export type MemoryDocument = z.infer<typeof memoryDocumentSchema>;
export type MemoryVersionSummary = z.infer<typeof memoryVersionSummarySchema>;
export type MemoryVersionDetail = z.infer<typeof memoryVersionDetailSchema>;
export type MemoryDreamResult = z.infer<typeof memoryDreamResultSchema>;

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
