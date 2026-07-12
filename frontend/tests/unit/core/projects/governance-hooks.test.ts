import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  commitCurrentProjectMutationCallback,
  createProjectMutationScope,
  currentProjectMutationObserverState,
  currentScopedGovernanceData,
  invalidateProjectGovernanceQueries,
} from "@/core/projects/hooks";
import {
  projectDetailKey,
  projectInvitationKey,
  projectKeys,
  projectMembersKey,
} from "@/core/projects/query-keys";

describe("project governance query scope", () => {
  test("keys every workspace and project governance query by account", () => {
    expect(projectKeys.workspace("u1")).toEqual(["account", "u1", "projects"]);
    expect(projectMembersKey("u1", "p1")).toEqual([
      "account",
      "u1",
      "project",
      "p1",
      "members",
    ]);
    expect(projectInvitationKey("u1", "p1")).toEqual([
      "account",
      "u1",
      "project",
      "p1",
      "invitations",
    ]);
    expect(projectKeys.myInvitations("u1")).toEqual([
      "account",
      "u1",
      "project-invitations",
      "mine",
    ]);
    expect(projectMembersKey("u1", "p1")).not.toEqual(
      projectMembersKey("u2", "p1"),
    );
  });

  test("cancels before invalidating every related key for a current mutation", async () => {
    const client = new QueryClient();
    const scope = createProjectMutationScope("u1", "p1");
    const token = scope.begin();
    const order: string[] = [];
    const cancel = rs
      .spyOn(client, "cancelQueries")
      .mockImplementation(async () => {
        order.push("cancel");
      });
    const invalidate = rs
      .spyOn(client, "invalidateQueries")
      .mockImplementation(async () => {
        order.push("invalidate");
      });

    await expect(
      invalidateProjectGovernanceQueries(client, scope, token),
    ).resolves.toBe(true);

    expect(order.slice(0, 5)).toEqual(Array(5).fill("cancel"));
    expect(order.slice(5)).toEqual(Array(5).fill("invalidate"));
    expect(cancel).toHaveBeenCalledWith({
      queryKey: projectKeys.workspace("u1"),
    });
    expect(cancel).toHaveBeenCalledWith({
      queryKey: projectDetailKey("u1", "p1"),
    });
    expect(cancel).toHaveBeenCalledWith({
      queryKey: projectMembersKey("u1", "p1"),
    });
    expect(cancel).toHaveBeenCalledWith({
      queryKey: projectInvitationKey("u1", "p1"),
    });
    expect(cancel).toHaveBeenCalledWith({
      queryKey: projectKeys.myInvitations("u1"),
    });
    expect(invalidate).toHaveBeenCalledTimes(5);
  });

  test("drops a late response after an account or project switch", async () => {
    const client = new QueryClient();
    const cancel = rs.spyOn(client, "cancelQueries");
    const invalidate = rs.spyOn(client, "invalidateQueries");
    const scope = createProjectMutationScope("u1", "p1");
    const oldAccount = scope.begin();
    scope.update("u2", "p2");

    await expect(
      invalidateProjectGovernanceQueries(client, scope, oldAccount),
    ).resolves.toBe(false);
    expect(cancel).not.toHaveBeenCalled();
    expect(invalidate).not.toHaveBeenCalled();
  });

  test("hides late mutation data after the rendered account or project changes", () => {
    const scope = createProjectMutationScope("u1", "p1");
    const token = scope.begin();
    const result = {
      data: { invite_url_fragment: "/invite#token=secret" },
      token,
    };

    expect(currentScopedGovernanceData(scope, result, "u1", "p1")).toEqual(
      result.data,
    );
    expect(
      currentScopedGovernanceData(scope, result, "u2", "p1"),
    ).toBeUndefined();
    expect(
      currentScopedGovernanceData(scope, result, "u1", "p2"),
    ).toBeUndefined();
  });

  test.each(["success", "error", "pending"] as const)(
    "hides the complete stale %s observer state after identity changes",
    (status) => {
      const scope = createProjectMutationScope("u1", "p1");
      const token = scope.begin();
      const rawState = {
        data: status === "success" ? { ok: true } : undefined,
        error: status === "error" ? new Error("old failure") : null,
        failureCount: status === "error" ? 1 : 0,
        failureReason: status === "error" ? new Error("old failure") : null,
        isError: status === "error",
        isIdle: false,
        isPaused: false,
        isPending: status === "pending",
        isSuccess: status === "success",
        status,
        submittedAt: 123,
        variables: { version: 1 },
      };

      scope.update("u2", "p2");

      expect(
        currentProjectMutationObserverState(scope, token, rawState, "u2", "p2"),
      ).toMatchObject({
        data: undefined,
        error: null,
        failureCount: 0,
        failureReason: null,
        isError: false,
        isIdle: true,
        isPaused: false,
        isPending: false,
        isSuccess: false,
        status: "idle",
        submittedAt: 0,
        variables: undefined,
      });
    },
  );

  test("rejects both success and failure callbacks from an old identity or attempt", () => {
    const scope = createProjectMutationScope("u1", "p1");
    const oldIdentity = scope.begin();
    const callback = rs.fn();

    scope.update("u2", "p2");
    expect(
      commitCurrentProjectMutationCallback(
        scope,
        oldIdentity,
        "u2",
        "p2",
        true,
        callback,
      ),
    ).toBe(false);
    expect(
      commitCurrentProjectMutationCallback(
        scope,
        oldIdentity,
        "u2",
        "p2",
        false,
        callback,
      ),
    ).toBe(false);
    expect(callback).not.toHaveBeenCalled();

    const oldAttempt = scope.begin();
    expect(
      commitCurrentProjectMutationCallback(
        scope,
        oldAttempt,
        "u2",
        "p2",
        false,
        callback,
      ),
    ).toBe(false);
    expect(callback).not.toHaveBeenCalled();
  });
});
