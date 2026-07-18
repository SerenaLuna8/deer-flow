"use client";

import { useQuery } from "@tanstack/react-query";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type {
  PrivateWorkAccess,
  ProjectClientScope,
} from "@/core/private-work/types";

import {
  automationBaseURL,
  readAutomationResponse,
  requestAutomation,
} from "./api";
import { automationQueryKey } from "./query-keys";
import { automationReadinessSchema, type AutomationReadiness } from "./types";

function requiredScope(access: PrivateWorkAccess): ProjectClientScope {
  return access.scope;
}

export async function fetchAutomationReadiness(
  scope: ProjectClientScope,
  signal?: AbortSignal,
): Promise<AutomationReadiness> {
  const response = await requestAutomation(
    `${automationBaseURL(scope)}/readiness`,
    { signal },
  );
  return readAutomationResponse(response, automationReadinessSchema);
}

export function projectAutomationReadinessOptions(
  access: PrivateWorkAccess,
  enabled = true,
) {
  const scope = access.scope;
  return {
    queryKey: automationQueryKey(scope, "readiness"),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAutomationReadiness(requiredScope(access), signal),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function automationManagementReady(
  canManage: boolean,
  readinessStatus?: AutomationReadiness["status"],
): boolean {
  return canManage && readinessStatus === "ready";
}

export function useProjectAutomationReadiness(enabled = true) {
  const access = usePrivateWorkAccess();
  return useQuery(projectAutomationReadinessOptions(access, enabled));
}
