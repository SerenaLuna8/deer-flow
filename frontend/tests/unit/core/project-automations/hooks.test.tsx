import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import type { PrivateWorkAccess } from "@/core/private-work/types";
import {
  automationMutationOptions,
  projectAutomationsQueryOptions,
} from "@/core/project-automations/hooks";
import { automationRoot } from "@/core/project-automations/query-keys";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

function access(scope: typeof SCOPE | null, active = true): PrivateWorkAccess {
  return {
    scope,
    apiBaseURL: scope
      ? `/api/projects/${scope.projectId}/private-work`
      : "/api",
    client: {} as PrivateWorkAccess["client"],
    queryKeyPrefix: [],
    reconnectOnMount: true,
    isActive: () => active,
  };
}

describe("project automation hooks", () => {
  test("derives list options only from the current private-work provider scope", () => {
    const active = projectAutomationsQueryOptions(access(SCOPE));
    expect(active.queryKey).toEqual([...automationRoot(SCOPE), "list", 50, 0]);
    expect(active.enabled).toBe(true);

    const inactive = projectAutomationsQueryOptions(access(null));
    expect(inactive.queryKey).toEqual(["automations", "inactive", "list"]);
    expect(inactive.enabled).toBe(false);
  });

  test("uses a scoped mutation key and forwards the provider abort signal", async () => {
    const controller = new AbortController();
    const scopedAccess = access(SCOPE);
    scopedAccess.runAbortable = (operation) => operation(controller.signal);
    const operation = rs.fn(async (_scope, _input: string, signal) => {
      expect(signal).toBe(controller.signal);
      return "created";
    });
    const queryClient = new QueryClient();
    const options = automationMutationOptions(
      queryClient,
      scopedAccess,
      "create",
      operation,
    );

    expect(options.mutationKey).toEqual([
      ...automationRoot(SCOPE),
      "mutation",
      "create",
    ]);
    await expect(options.mutationFn("input")).resolves.toBe("created");
    expect(operation).toHaveBeenCalledWith(SCOPE, "input", controller.signal);
  });

  test("does not run without scope or invalidate after the scope becomes stale", async () => {
    const queryClient = new QueryClient();
    const invalidate = rs.spyOn(queryClient, "invalidateQueries");
    const operation = rs.fn(async () => "result");
    const noScope = automationMutationOptions(
      queryClient,
      access(null),
      "create",
      operation,
    );
    await expect(noScope.mutationFn("input")).rejects.toThrow(
      "Project automation scope is unavailable",
    );
    expect(operation).not.toHaveBeenCalled();

    const stale = automationMutationOptions(
      queryClient,
      access(SCOPE, false),
      "pause",
      operation,
    );
    await stale.onSuccess();
    expect(invalidate).not.toHaveBeenCalled();
  });

  test("invalidates only the originating account/project root on current success", async () => {
    const queryClient = new QueryClient();
    const invalidate = rs.spyOn(queryClient, "invalidateQueries");
    const options = automationMutationOptions(
      queryClient,
      access(SCOPE),
      "resume",
      async () => "result",
    );

    await options.onSuccess();
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: automationRoot(SCOPE),
    });
  });
});
