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

const memorySummarySchema = z
  .object({ summary: z.string(), updatedAt: z.string() })
  .strict();

const memoryFactSchema = z
  .object({
    id: z.string().min(1),
    content: z.string(),
    category: z.string(),
    confidence: z.number().min(0).max(1),
    createdAt: z.string(),
    source: z.string(),
    sourceThreadId: z.string().optional(),
    sourceRunId: z.string().optional(),
  })
  .strict();

export const userMemorySchema = z
  .object({
    version: z.string(),
    lastUpdated: z.string(),
    user: z
      .object({
        workContext: memorySummarySchema,
        personalContext: memorySummarySchema,
        topOfMind: memorySummarySchema,
      })
      .strict(),
    history: z
      .object({
        recentMonths: memorySummarySchema,
        earlierContext: memorySummarySchema,
        longTermBackground: memorySummarySchema,
      })
      .strict(),
    facts: z.array(memoryFactSchema),
  })
  .strict();

export type MemoryFact = z.infer<typeof memoryFactSchema>;
export type UserMemory = z.infer<typeof userMemorySchema>;
export type MemoryFactInput = Pick<
  MemoryFact,
  "content" | "category" | "confidence"
>;
export type MemoryFactPatchInput = Partial<MemoryFactInput>;

const projectMemoryResponseSchema = z
  .object({
    namespace: z.string().min(1),
    version: z.number().int().nonnegative(),
    memory: userMemorySchema,
  })
  .strict();

export type ProjectMemorySnapshot = z.infer<typeof projectMemoryResponseSchema>;

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

async function readProjectMemoryResponse(
  response: Response,
  fallback: string,
): Promise<ProjectMemorySnapshot> {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return projectMemoryResponseSchema.parse(await response.json());
}

function namespacePath(baseURL: string, path = "") {
  return `${baseURL}${path}?namespace=default`;
}

export function projectMemoryQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory", "default");
}

export function projectMemoryMutationKey(
  scope: ProjectClientScope,
  action: "reload" | "import" | "update-fact" | "delete-fact",
) {
  return privateWorkQueryKey(scope, "memory", "default", "mutation", action);
}

export function projectMemoryPermissions(capabilities: readonly Capability[]) {
  const canRead = capabilities.includes("private_work.read_own");
  const canModify = capabilities.includes("private_work.create");
  return {
    canRead,
    canExport: canRead,
    canReload: canModify,
    canImport: canModify,
    canModify,
    canDelete: canModify,
  };
}

export async function loadProjectMemory(
  access: ProjectMemoryAccess,
  signal?: AbortSignal,
) {
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(namespacePath(baseURL), { signal });
  return readProjectMemoryResponse(response, "Failed to load project Memory");
}

export async function exportProjectMemory(
  access: ProjectMemoryAccess,
  signal?: AbortSignal,
): Promise<UserMemory> {
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(namespacePath(baseURL, "/export"), {
    signal,
  });
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to export project Memory");
  }
  return userMemorySchema.parse(await response.json());
}

export async function importProjectMemory(
  access: ProjectMemoryAccess,
  expectedVersion: number,
  memory: UserMemory,
  signal?: AbortSignal,
) {
  const parsedMemory = userMemorySchema.parse(memory);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(namespacePath(baseURL, "/import"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_version: z.number().int().nonnegative().parse(expectedVersion),
      memory: parsedMemory,
    }),
    signal,
  });
  return readProjectMemoryResponse(response, "Failed to import project Memory");
}

export async function reloadProjectMemory(
  access: ProjectMemoryAccess,
  signal?: AbortSignal,
) {
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(namespacePath(baseURL, "/reload"), {
    method: "POST",
    signal,
  });
  return readProjectMemoryResponse(response, "Failed to reload project Memory");
}

export async function updateProjectMemoryFact(
  access: ProjectMemoryAccess,
  factId: string,
  expectedVersion: number,
  input: { content?: string; category?: string; confidence?: number },
  signal?: AbortSignal,
) {
  const parsedInput = z
    .object({
      content: z.string().min(1).optional(),
      category: z.string().optional(),
      confidence: z.number().min(0).max(1).optional(),
    })
    .strict()
    .refine((value) => Object.keys(value).length > 0)
    .parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    namespacePath(baseURL, `/facts/${encodeURIComponent(factId)}`),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_version: z.number().int().nonnegative().parse(expectedVersion),
        ...parsedInput,
      }),
      signal,
    },
  );
  return readProjectMemoryResponse(response, "Failed to update project Memory");
}

export async function deleteProjectMemoryFact(
  access: ProjectMemoryAccess,
  factId: string,
  expectedVersion: number,
  signal?: AbortSignal,
) {
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    namespacePath(baseURL, `/facts/${encodeURIComponent(factId)}`),
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_version: z.number().int().nonnegative().parse(expectedVersion),
      }),
      signal,
    },
  );
  return readProjectMemoryResponse(response, "Failed to delete project Memory");
}
