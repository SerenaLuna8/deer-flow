import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: rs.fn(),
}));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import { automationRoot } from "@/core/project-automations/query-keys";
import {
  automationManagementReady,
  fetchAutomationReadiness,
  projectAutomationReadinessOptions,
} from "@/core/project-automations/readiness";

import { AUTOMATION_READINESS } from "./fixtures";

const mockedFetch = rs.mocked(fetchWithAuth);
const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

function access(scope: typeof SCOPE | null): PrivateWorkAccess {
  return {
    scope,
    apiBaseURL: scope
      ? `/api/projects/${scope.projectId}/private-work`
      : "/api",
    client: {} as PrivateWorkAccess["client"],
    queryKeyPrefix: [],
    reconnectOnMount: true,
  };
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project automation readiness", () => {
  test("loads a strict readiness response from the project automation base", async () => {
    const signal = new AbortController().signal;
    mockedFetch.mockResolvedValueOnce(Response.json(AUTOMATION_READINESS));

    await expect(fetchAutomationReadiness(SCOPE, signal)).resolves.toEqual(
      AUTOMATION_READINESS,
    );
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${SCOPE.projectId}/automations/readiness`,
      { signal },
    );

    mockedFetch.mockResolvedValueOnce(
      Response.json({ ...AUTOMATION_READINESS, ownership_key: "private" }),
    );
    await expect(fetchAutomationReadiness(SCOPE)).rejects.toMatchObject({
      code: "AUTOMATION_RESPONSE_INVALID",
    });
  });

  test("disables readiness without current provider identity", () => {
    const active = projectAutomationReadinessOptions(access(SCOPE));
    expect(active.queryKey).toEqual([...automationRoot(SCOPE), "readiness"]);
    expect(active.enabled).toBe(true);

    const inactive = projectAutomationReadinessOptions(access(null));
    expect(inactive.queryKey).toEqual(["automations", "inactive", "readiness"]);
    expect(inactive.enabled).toBe(false);
  });

  test("requires both server readiness and automation capability", () => {
    expect(automationManagementReady(true, "ready")).toBe(true);
    expect(automationManagementReady(false, "ready")).toBe(false);
    expect(automationManagementReady(true, "migration_required")).toBe(false);
    expect(automationManagementReady(true, "unavailable")).toBe(false);
    expect(automationManagementReady(true, undefined)).toBe(false);
  });
});
