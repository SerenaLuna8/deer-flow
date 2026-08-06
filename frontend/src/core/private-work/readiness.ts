"use client";

import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";

import { usePrivateWorkAccess } from "./provider";
import type { PrivateWorkAccess } from "./types";

const projectPrivateWorkReadinessSchema = z
  .object({
    status: z.enum(["ready", "unavailable"]),
    code: z.string().min(1),
    request_id: z.string().min(1),
  })
  .strict();

export type ProjectPrivateWorkReadiness = z.infer<
  typeof projectPrivateWorkReadinessSchema
>;

export async function fetchProjectPrivateWorkReadiness(
  access: Pick<PrivateWorkAccess, "apiBaseURL">,
  signal?: AbortSignal,
): Promise<ProjectPrivateWorkReadiness> {
  const response = await fetchWithAuth(`${access.apiBaseURL}/readiness`, {
    signal,
  });
  if (!response.ok) {
    throw new Error("Failed to load project private-work readiness.");
  }
  return projectPrivateWorkReadinessSchema.parse(await response.json());
}

export function projectPrivateWorkEntryEnabled(
  featureEnabled: boolean,
  canCreate: boolean,
  readinessStatus?: ProjectPrivateWorkReadiness["status"],
): boolean {
  return featureEnabled && canCreate && readinessStatus === "ready";
}

export function useProjectPrivateWorkReadiness(enabled = true) {
  const access = usePrivateWorkAccess();
  return useQuery({
    queryKey: [...access.queryKeyPrefix, "readiness"],
    queryFn: ({ signal }) => fetchProjectPrivateWorkReadiness(access, signal),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  });
}
