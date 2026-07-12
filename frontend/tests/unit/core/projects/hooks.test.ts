import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  commitProjectMutation,
  createProjectMutationScope,
  requireProjectIdentity,
} from "@/core/projects/hooks";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha-project",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: [...CAPABILITIES],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace-1",
};

describe("project mutation account scope", () => {
  test("aborts every independent request on account or project change", () => {
    const scope = createProjectMutationScope("u1", "p1");
    const first = scope.begin();
    const second = scope.begin();
    expect(first.signal).not.toBe(second.signal);
    scope.update("u2", "p2");
    expect(first.signal.aborted).toBe(true);
    expect(second.signal.aborted).toBe(true);
    expect(scope.isCurrent(first)).toBe(false);

    const afterChange = scope.begin();
    scope.dispose();
    expect(afterChange.signal.aborted).toBe(true);
    expect(scope.isCurrent(afterChange)).toBe(false);
  });

  test("drops stale mutation responses without touching any account cache", async () => {
    const client = new QueryClient();
    const invalidate = rs.spyOn(client, "invalidateQueries");
    const scope = createProjectMutationScope("u1", "p1");
    const token = scope.begin();
    scope.update("u2", "p2");
    await commitProjectMutation(client, scope, token, project);
    expect(
      client.getQueryData(["account", "u1", "project", project.id, "detail"]),
    ).toBeUndefined();
    expect(
      client.getQueryData(["account", "u2", "project", project.id, "detail"]),
    ).toBeUndefined();
    expect(invalidate).not.toHaveBeenCalled();
  });

  test("commits a current create response only to the current account", async () => {
    const client = new QueryClient();
    client.setQueryData(["account", "u2", "projects"], "keep-u2");
    const invalidate = rs.spyOn(client, "invalidateQueries");
    const scope = createProjectMutationScope("u1", null);
    const token = scope.begin();
    await commitProjectMutation(client, scope, token, project);
    expect(
      client.getQueryData(["account", "u1", "project", project.id, "detail"]),
    ).toEqual(project);
    expect(client.getQueryData(["account", "u2", "projects"])).toBe("keep-u2");
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["account", "u1", "projects"],
    });
  });

  test("drops a response that does not match the scoped route project", async () => {
    const client = new QueryClient();
    const invalidate = rs.spyOn(client, "invalidateQueries");
    const scope = createProjectMutationScope("u1", "different-project-id");
    const token = scope.begin();
    expect(await commitProjectMutation(client, scope, token, project)).toBe(
      false,
    );
    expect(invalidate).not.toHaveBeenCalled();
  });

  test("requires account and project identity before any hook query can fetch", () => {
    expect(() => requireProjectIdentity(null)).toThrowError(
      "Authentication required",
    );
    expect(() => requireProjectIdentity("u1", null, true)).toThrowError(
      "Project validation failed",
    );
    expect(requireProjectIdentity("u1", project.id, true)).toEqual({
      userId: "u1",
      projectId: project.id,
    });
  });
});
