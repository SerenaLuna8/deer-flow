import { z } from "zod";

import { privateWorkQueryKey } from "../query-keys";
import type { ProjectClientScope } from "../types";

import {
  memoryEpisodesFilterSchema,
  memoryVersionPageInputSchema,
  memoryVersionSchema,
} from "./schemas";
import type { MemoryEpisodesFilter, MemoryVersionPageInput } from "./types";

export function projectMemoryRootQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory");
}

export function projectMemoryDocumentQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "memory", "document");
}

export function projectMemoryVersionsQueryKey(
  scope: ProjectClientScope,
  input: MemoryVersionPageInput,
) {
  const parameters = memoryVersionPageInputSchema.parse(input);
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
  return privateWorkQueryKey(
    scope,
    "memory",
    "version",
    memoryVersionSchema.parse(version),
  );
}

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

export function projectMemoryDreamPreparationQueryKey(
  scope: ProjectClientScope,
  jobId: string,
) {
  const parsedJobId = z.string().uuid().parse(jobId);
  return privateWorkQueryKey(scope, "memory", "dream-preparation", parsedJobId);
}

export function projectMemoryLatestDreamPreparationQueryKey(
  scope: ProjectClientScope,
  threadId: string,
) {
  const parsedThreadId = z.string().min(1).max(64).parse(threadId);
  return privateWorkQueryKey(
    scope,
    "memory",
    "dream-preparation",
    "latest",
    parsedThreadId,
  );
}

export function projectMemoryMutationKey(
  scope: ProjectClientScope,
  action: "dream" | "dream-prepare" | "dream-prepare-cancel" | "restore",
) {
  return privateWorkQueryKey(scope, "memory", "mutation", action);
}
