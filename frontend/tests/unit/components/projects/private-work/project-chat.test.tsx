import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectChatNotFound,
  projectChatRouteScope,
  projectThreadAvailability,
} from "@/components/projects/private-work/project-chat-page";
import { ProjectPrivateWorkCta } from "@/components/projects/project-private-work-cta";
import { CAPABILITIES, type Project } from "@/core/projects/types";

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
    expect(scope.scheduledTasksVisible).toBe(false);
    expect(scope.goalVisible).toBe(false);
    expect(scope.compactVisible).toBe(false);
    expect(scope.branchVisible).toBe(false);
    expect(scope.regenerateVisible).toBe(false);
    expect(scope.sidecarVisible).toBe(true);
    expect(scope.artifactsVisible).toBe(true);
    expect(scope.followupSuggestionsEnabled).toBe(false);
  });

  test("viewer sees read-only guidance and never gets a create control", () => {
    const viewer: Project = {
      ...project,
      role: "viewer" as const,
      capabilities: ["project.read", "private_work.read_own"],
    };
    const html = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkCta project={viewer} />
      </QueryClientProvider>,
    );
    expect(html).toContain("你可以查看自己的既有对话，但不能创建新工作");
    expect(html).not.toContain("开始私有对话");
  });

  test("project routes consume entered project context and never list projects", () => {
    for (const file of [
      "src/app/projects/[project_slug]/chats/page.tsx",
      "src/app/projects/[project_slug]/chats/[thread_id]/page.tsx",
    ]) {
      const source = readFileSync(resolve(process.cwd(), file), "utf8");
      expect(source).toContain("useCurrentProject");
      expect(source).not.toMatch(
        /useProjectBySlug|useProjects|useEnterProject/u,
      );
    }
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
    const html = renderToStaticMarkup(<ProjectChatNotFound />);
    expect(html).toContain("找不到这个对话");
    expect(html).not.toMatch(/owner|project|跨项目|其他用户/iu);
  });

  test("workspace and project details share the scoped chat implementation", () => {
    const shared = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/chats/scoped-chat-page.tsx",
      ),
      "utf8",
    );
    const workspace = readFileSync(
      resolve(process.cwd(), "src/app/workspace/chats/[thread_id]/page.tsx"),
      "utf8",
    );
    const projectRoute = readFileSync(
      resolve(
        process.cwd(),
        "src/app/projects/[project_slug]/chats/[thread_id]/page.tsx",
      ),
      "utf8",
    );

    expect(workspace).toContain("ScopedChatPage");
    expect(projectRoute).toContain("ProjectChatPage");
    const projectPage = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/private-work/project-chat-page.tsx",
      ),
      "utf8",
    );
    expect(projectPage).toContain("ScopedChatPage");
    const projectProviders = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/private-work/project-chat-providers.tsx",
      ),
      "utf8",
    );
    expect(projectProviders).toContain("StandaloneArtifactsProvider");
    expect(projectProviders).toContain("enabled={true}");
    expect(projectProviders).not.toMatch(/<ArtifactsProvider>/u);
    expect(shared).toContain("useThreadStream");
    expect(shared).toContain("handleStop");
    expect(shared).toContain("handleSubmitHumanInput");
    expect(shared).toContain("loadMoreHistory");
    expect(shared).toContain("scope.canUpload");
    expect(shared).toContain("scope.regenerateVisible");
    expect(shared).toContain("scope.branchVisible");
    expect(shared).toContain("scope.sidecarVisible");
    expect(shared).toContain("scope.artifactsVisible");
    expect(shared).toContain("scope.sidebarTriggerVisible");
    expect(shared).toContain("scope.followupSuggestionsEnabled");
    expect(shared).toContain("enabled={scope.sidecarVisible !== false}");
    expect(projectPage).toContain("sidebarTriggerVisible: false");
    expect(projectPage).toContain("sidecarVisible: canRun");
    expect(projectPage).toContain("artifactsVisible: canRead");
  });

  test("viewer can open scoped files without gaining sidecar runs or uploads", () => {
    const viewer: Project = {
      ...project,
      role: "viewer",
      capabilities: ["project.read", "private_work.read_own"],
    };
    const scope = projectChatRouteScope(viewer);
    expect(scope.canUpload).toBe(false);
    expect(scope.sidecarVisible).toBe(false);
    expect(scope.artifactsVisible).toBe(true);
  });

  test("project list uses the scoped delete hook without nesting actions in links", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/private-work/project-chats-page.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("usePrivateWorkAccess");
    expect(source).toContain("useDeleteThread(privateWork)");
    expect(source).toContain("aria-label={`删除 ${titleOfThread(thread)}`}");
    expect(source).toMatch(/<\/Link>\s*\{canDelete && \(/u);
  });
});
