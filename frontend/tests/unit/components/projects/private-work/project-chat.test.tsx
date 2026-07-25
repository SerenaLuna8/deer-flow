import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectChatNotFound,
  projectChatRouteScope,
  projectThreadAvailability,
} from "@/components/projects/private-work/project-chat-page";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const THREAD_ID = "33333333-3333-4333-8333-333333333333";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "runner",
  capabilities: [...CAPABILITIES],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 1,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "req-project",
};

describe("project chat route", () => {
  test("derives project capability gates without exposing unsupported actions", () => {
    const scope = projectChatRouteScope(project);
    expect(scope.threadBasePath).toBe("/projects/alpha/chats");
    expect(scope.newThreadPath).toBe("/projects/alpha/chats");
    expect(scope.canCreate).toBe(true);
    expect(scope.canRun).toBe(true);
    expect(scope.canUpload).toBe(true);
    expect(scope.canDelete).toBe(true);
    expect(scope.canDeleteFiles).toBe(true);
    expect(scope.automationVisible).toBe(false);
    expect(scope.goalVisible).toBe(true);
    expect(scope.compactVisible).toBe(true);
    expect(scope.branchVisible).toBe(true);
    expect(scope.regenerateVisible).toBe(true);
    expect(scope.sidecarVisible).toBe(true);
    expect(scope.artifactsVisible).toBe(true);
    expect(scope.followupSuggestionsEnabled).toBe(true);
  });

  test("uses only the project Automation route when every Chat entry gate is open", () => {
    const scope = projectChatRouteScope(project, true, true, false);
    expect(scope.automationVisible).toBe(true);
    expect(scope.automationHref(THREAD_ID)).toBe(
      `/projects/alpha/automations?thread_id=${THREAD_ID}`,
    );

    const encodedScope = projectChatRouteScope(
      { ...project, slug: "alpha/beta" },
      true,
      true,
      false,
    );
    expect(encodedScope.automationHref(THREAD_ID)).toBe(
      `/projects/alpha%2Fbeta/automations?thread_id=${THREAD_ID}`,
    );
  });

  test("allows a read-only Viewer Chat entry but fails closed for every missing gate", () => {
    const viewer: Project = {
      ...project,
      role: "viewer",
      capabilities: ["project.read", "private_work.read_own"],
    };
    expect(projectChatRouteScope(viewer, true, true, false)).toMatchObject({
      automationVisible: true,
    });
    expect(projectChatRouteScope(viewer, false, true, false)).toMatchObject({
      automationVisible: false,
    });
    expect(projectChatRouteScope(viewer, true, false, false)).toMatchObject({
      automationVisible: false,
    });
    expect(projectChatRouteScope(viewer, true, true, true)).toMatchObject({
      automationVisible: false,
    });
    expect(
      projectChatRouteScope(
        { ...viewer, capabilities: ["project.read"] },
        true,
        true,
        false,
      ),
    ).toMatchObject({ automationVisible: false });
  });

  test("same-project other-owner and cross-project metadata misses share not-found", () => {
    expect(
      projectThreadAvailability({
        data: null,
        error: null,
        isLoading: false,
        isFetching: false,
      }),
    ).toBe("not-found");
    expect(
      projectThreadAvailability({
        data: undefined,
        error: new Error("NETWORK_FAILURE"),
        isLoading: false,
        isFetching: false,
      }),
    ).toBe("error");
    expect(
      projectThreadAvailability({
        data: null,
        error: new Error("REFETCH_FAILURE"),
        isLoading: false,
        isFetching: false,
      }),
    ).toBe("error");
    const html = renderToStaticMarkup(
      <ProjectChatNotFound chatsPath="/projects/research-lab/chats" />,
    );
    expect(html).toContain("找不到这个对话");
    expect(html).toContain('href="/projects/research-lab/chats"');
    expect(html).not.toMatch(/owner|跨项目|其他用户/iu);
  });

  test("viewer can open scoped files without gaining sidecar runs or uploads", () => {
    const viewer: Project = {
      ...project,
      role: "viewer",
      capabilities: ["project.read", "private_work.read_own"],
    };
    const scope = projectChatRouteScope(viewer);
    expect(scope.canUpload).toBe(false);
    expect(scope.canDeleteFiles).toBe(true);
    expect(scope.goalVisible).toBe(true);
    expect(scope.compactVisible).toBe(false);
    expect(scope.branchVisible).toBe(false);
    expect(scope.regenerateVisible).toBe(false);
    expect(scope.sidecarVisible).toBe(false);
    expect(scope.artifactsVisible).toBe(true);
    expect(scope.followupSuggestionsEnabled).toBe(false);
  });
});
