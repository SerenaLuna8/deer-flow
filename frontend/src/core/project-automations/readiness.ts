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

const INACTIVE_READINESS_KEY = [
  "automations",
  "inactive",
  "readiness",
] as const;

function requiredScope(access: PrivateWorkAccess): ProjectClientScope {
  if (!access.scope) {
    throw new Error("Project automation scope is unavailable");
  }
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
    queryKey: scope
      ? automationQueryKey(scope, "readiness")
      : INACTIVE_READINESS_KEY,
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAutomationReadiness(requiredScope(access), signal),
    enabled: enabled && scope !== null,
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
