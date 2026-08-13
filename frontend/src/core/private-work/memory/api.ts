import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

import { projectClientScopeSchema } from "../types";

import {
  memoryDocumentSchema,
  memoryDreamInputSchema,
  memoryDreamPreparationAdmissionSchema,
  memoryDreamPreparationInputSchema,
  memoryDreamPreparationStatusSchema,
  memoryDreamResultSchema,
  memoryEpisodesInputSchema,
  memoryEpisodesSchema,
  memoryPendingInputSchema,
  memoryPendingSchema,
  memoryRestoreInputSchema,
  memoryVersionDetailSchema,
  memoryVersionPageInputSchema,
  memoryVersionsSchema,
  memoryVersionSchema,
} from "./schemas";
import type {
  MemoryDreamInput,
  MemoryDreamPreparationInput,
  MemoryEpisodesInput,
  MemoryPendingInput,
  MemoryRestoreInput,
  MemoryVersionPageInput,
  ProjectMemoryAccess,
} from "./types";

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

async function readJSON<TSchema extends z.ZodType>(
  response: Response,
  schema: TSchema,
  fallback: string,
): Promise<z.output<TSchema>> {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return schema.parse(await response.json());
}

export async function getProjectMemory(
  access: ProjectMemoryAccess,
  signal?: AbortSignal,
) {
  const { baseURL } = requireProjectMemoryAccess(access);
  const search = new URLSearchParams({ injectionContract: "advisory_v1" });
  const response = await fetchWithAuth(`${baseURL}?${search}`, { signal });
  return readJSON(
    response,
    memoryDocumentSchema,
    "Failed to load project Memory",
  );
}

export async function dreamProjectMemory(
  access: ProjectMemoryAccess,
  input: MemoryDreamInput = {},
  signal?: AbortSignal,
) {
  const body = memoryDreamInputSchema.parse(input);
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

export async function admitProjectMemoryDreamPreparation(
  access: ProjectMemoryAccess,
  input: MemoryDreamPreparationInput,
  signal?: AbortSignal,
) {
  const body = memoryDreamPreparationInputSchema.parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(`${baseURL}/dream-preparations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return readJSON(
    response,
    memoryDreamPreparationAdmissionSchema,
    "Failed to start Dream preparation",
  );
}

export async function getProjectMemoryDreamPreparation(
  access: ProjectMemoryAccess,
  jobId: string,
  signal?: AbortSignal,
) {
  const parsedJobId = z.string().uuid().parse(jobId);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    `${baseURL}/dream-preparations/${parsedJobId}`,
    { signal },
  );
  return readJSON(
    response,
    memoryDreamPreparationStatusSchema,
    "Failed to load Dream preparation",
  );
}

export async function getLatestProjectMemoryDreamPreparation(
  access: ProjectMemoryAccess,
  threadId: string,
  signal?: AbortSignal,
) {
  const parsedThreadId = z.string().min(1).max(64).parse(threadId);
  const { baseURL } = requireProjectMemoryAccess(access);
  const search = new URLSearchParams({ threadId: parsedThreadId });
  const response = await fetchWithAuth(
    `${baseURL}/dream-preparations/latest?${search}`,
    { signal },
  );
  return readJSON(
    response,
    memoryDreamPreparationStatusSchema,
    "Failed to recover Dream preparation",
  );
}

export async function cancelProjectMemoryDreamPreparation(
  access: ProjectMemoryAccess,
  jobId: string,
  signal?: AbortSignal,
) {
  const parsedJobId = z.string().uuid().parse(jobId);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    `${baseURL}/dream-preparations/${parsedJobId}/cancel`,
    { method: "POST", signal },
  );
  return readJSON(
    response,
    memoryDreamPreparationStatusSchema,
    "Failed to cancel Dream preparation",
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
  search.set("pagination", "keyset_v1");
  if (parameters.q !== undefined) search.set("q", parameters.q);
  for (const tag of parameters.tags ?? []) search.append("tags", tag);
  if (parameters.cursor !== undefined) search.set("cursor", parameters.cursor);
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
  input: MemoryPendingInput = {},
  signal?: AbortSignal,
) {
  const parameters = memoryPendingInputSchema.parse(input);
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
  input: MemoryVersionPageInput,
  signal?: AbortSignal,
) {
  const parameters = memoryVersionPageInputSchema.parse(input);
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
  const parsedVersion = memoryVersionSchema.parse(version);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    `${baseURL}/versions/${parsedVersion}?responseContract=preview_v1`,
    { signal },
  );
  return readJSON(
    response,
    memoryVersionDetailSchema,
    "Failed to load project Memory version",
  );
}

export async function restoreProjectMemoryVersion(
  access: ProjectMemoryAccess,
  version: number,
  input: MemoryRestoreInput,
  signal?: AbortSignal,
) {
  const parsedVersion = memoryVersionSchema.parse(version);
  const body = memoryRestoreInputSchema.parse(input);
  const { baseURL } = requireProjectMemoryAccess(access);
  const response = await fetchWithAuth(
    `${baseURL}/versions/${parsedVersion}/restore?responseContract=preview_v1`,
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
