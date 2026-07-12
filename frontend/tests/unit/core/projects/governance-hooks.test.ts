import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  createProjectMutationScope,
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
});
