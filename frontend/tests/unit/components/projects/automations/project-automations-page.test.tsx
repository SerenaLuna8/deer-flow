import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({
    user: {
      id: "11111111-1111-4111-8111-111111111111",
      email: "owner@example.com",
    },
  }),
}));
rs.mock("@/core/project-automations/hooks", () => ({
  useProjectAutomations: rs.fn(),
  useThreadProjectAutomations: rs.fn(),
  useProjectAutomationRuns: rs.fn(),
  useCreateProjectAutomation: rs.fn(),
  useUpdateProjectAutomation: rs.fn(),
  useDeleteProjectAutomation: rs.fn(),
  usePauseProjectAutomation: rs.fn(),
  useResumeProjectAutomation: rs.fn(),
  useTriggerProjectAutomation: rs.fn(),
}));
rs.mock("@/core/project-automations/readiness", () => ({
  useProjectAutomationReadiness: rs.fn(),
}));
rs.mock("@/core/shared-assets", () => ({
  useProjectAssets: rs.fn(),
}));

import {
  ProjectAutomationsPage,
  automationActionFeedback,
  automationFeedbackForProject,
  automationPermissions,
} from "@/components/projects/automations/project-automations-page";
import { AutomationApiError } from "@/core/project-automations/api";
import {
  useCreateProjectAutomation,
  useDeleteProjectAutomation,
  usePauseProjectAutomation,
  useProjectAutomationRuns,
  useProjectAutomations,
  useResumeProjectAutomation,
  useThreadProjectAutomations,
  useTriggerProjectAutomation,
  useUpdateProjectAutomation,
} from "@/core/project-automations/hooks";
import { useProjectAutomationReadiness } from "@/core/project-automations/readiness";
import type { AutomationReadiness } from "@/core/project-automations/types";
import type { Project } from "@/core/projects/types";
import { useProjectAssets } from "@/core/shared-assets";

import {
  AUTOMATION,
  AUTOMATION_READINESS,
  AUTOMATION_RUN,
} from "../../../core/project-automations/fixtures";

const PROJECT: Project = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.execute",
    "automation.manage_own",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 1,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

function mutation() {
  return { mutateAsync: rs.fn(async () => undefined), isPending: false };
}

function prepare({
  readiness = AUTOMATION_READINESS,
}: { readiness?: AutomationReadiness } = {}) {
  rs.mocked(useProjectAutomationReadiness).mockReturnValue({
    data: readiness,
    isLoading: false,
    error: null,
    refetch: rs.fn(async () => undefined),
  } as never);
  rs.mocked(useProjectAutomations).mockReturnValue({
    data: [AUTOMATION],
    isLoading: false,
    error: null,
    refetch: rs.fn(async () => undefined),
  } as never);
  rs.mocked(useThreadProjectAutomations).mockReturnValue({} as never);
  rs.mocked(useProjectAutomationRuns).mockReturnValue({
    data: [AUTOMATION_RUN],
    isLoading: false,
    error: null,
    refetch: rs.fn(async () => undefined),
  } as never);
  rs.mocked(useProjectAssets).mockReturnValue({
    data: { project_items: [], system_items: [], request_id: "req-agents" },
    isLoading: false,
    error: null,
  } as never);
  for (const hook of [
    useCreateProjectAutomation,
    useUpdateProjectAutomation,
    useDeleteProjectAutomation,
    usePauseProjectAutomation,
    useResumeProjectAutomation,
    useTriggerProjectAutomation,
  ]) {
    rs.mocked(hook).mockReturnValue(mutation() as never);
  }
}

describe("ProjectAutomationsPage", () => {
  test("derives permissions only from capabilities", () => {
    expect(automationPermissions(["private_work.read_own"])).toEqual({
      canRead: true,
      canManage: false,
      canExecute: false,
    });
    expect(
      automationPermissions([
        "private_work.read_own",
        "automation.manage_own",
        "private_work.create",
        "shared_assets.execute",
      ]),
    ).toEqual({ canRead: true, canManage: true, canExecute: true });
  });

  test.each([
    [
      new AutomationApiError(
        409,
        "AUTOMATION_VERSION_CONFLICT",
        "SELECT secret FROM internal",
      ),
      "conflict",
      "状态已更新，请刷新后重试。",
    ],
    [
      new AutomationApiError(
        429,
        "AUTOMATION_CONCURRENCY_LIMIT",
        "lease owner was internal",
      ),
      "rate_limit",
      "当前并发已达上限，请稍后重试。",
    ],
    [
      new AutomationApiError(
        503,
        "AUTOMATION_UNAVAILABLE",
        "postgres password was internal",
      ),
      "unavailable",
      "Automation 暂时不可用，请稍后重试。",
    ],
  ] as const)("maps %s to safe public feedback", (error, kind, message) => {
    expect(automationActionFeedback("update", error)).toEqual({
      action: "update",
      kind,
      message,
    });
    expect(message).not.toContain(error.message);
  });

  test("drops feedback from a previous project scope", () => {
    const feedback = {
      projectId: PROJECT.id,
      feedback: {
        action: "delete" as const,
        kind: "conflict" as const,
        message: "状态已更新，请刷新后重试。",
      },
    };

    expect(automationFeedbackForProject(feedback, PROJECT.id)).toBe(
      feedback.feedback,
    );
    expect(
      automationFeedbackForProject(
        feedback,
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      ),
    ).toBeNull();
  });

  test("shows scheduler disabled while keeping manual execution available", () => {
    prepare({
      readiness: {
        ...AUTOMATION_READINESS,
        scheduler_enabled: false,
        scheduler_status: "disabled",
      },
    });

    const html = renderToStaticMarkup(
      <ProjectAutomationsPage project={PROJECT} />,
    );

    expect(html).toContain('data-testid="automation-scheduler-disabled"');
    expect(html).toMatch(
      /<button(?![^>]*\sdisabled(?:=|>))[^>]*>立即运行<\/button>/u,
    );
    expect(useProjectAutomations).toHaveBeenLastCalledWith({}, true);
  });

  test.each([
    ["migration_required", "automation-migration-required"],
    ["unavailable", "automation-unavailable"],
  ] as const)("fails closed for %s readiness", (status, testId) => {
    prepare({
      readiness: {
        ...AUTOMATION_READINESS,
        status,
        code: `AUTOMATION_${status.toUpperCase()}`,
      },
    });

    const html = renderToStaticMarkup(
      <ProjectAutomationsPage project={PROJECT} />,
    );

    expect(html).toContain(`data-testid="${testId}"`);
    expect(useProjectAutomations).toHaveBeenLastCalledWith({}, false);
  });

  test.each([
    ["project_private_work_ready", false],
    ["automation_cutover_ready", false],
  ] as const)("fails closed when ready has %s=%s", (field, value) => {
    prepare({
      readiness: { ...AUTOMATION_READINESS, [field]: value },
    });

    const html = renderToStaticMarkup(
      <ProjectAutomationsPage project={PROJECT} />,
    );

    expect(html).toContain('data-testid="automation-unavailable"');
    expect(useProjectAutomations).toHaveBeenLastCalledWith({}, false);
  });

  test("renders Viewer data but does not enable Agent authority or mutations", () => {
    prepare();
    const viewer = {
      ...PROJECT,
      role: "viewer" as const,
      capabilities: [
        "project.read",
        "private_work.read_own",
      ] as Project["capabilities"],
    };

    const html = renderToStaticMarkup(
      <ProjectAutomationsPage project={viewer} />,
    );

    expect(html).toContain(AUTOMATION.title);
    expect(html).toContain('data-testid="automation-run-list"');
    expect(html).not.toContain(">立即运行<");
    expect(html).not.toContain(">删除<");
    expect(useProjectAssets).toHaveBeenLastCalledWith(
      "11111111-1111-4111-8111-111111111111",
      PROJECT.id,
      "agents",
      false,
    );
  });

  test("route consumes current project and does not resolve slug or use legacy APIs", () => {
    const route = readFileSync(
      resolve(
        process.cwd(),
        "src/app/projects/[project_slug]/automations/page.tsx",
      ),
      "utf8",
    );
    const page = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/automations/project-automations-page.tsx",
      ),
      "utf8",
    );

    expect(route).toContain("useCurrentProject");
    expect(route).not.toMatch(/useProjects|useProjectBySlug|useEnterProject/u);
    expect(page).not.toMatch(
      /core\/scheduled-tasks\/hooks|\/api\/scheduled-tasks/u,
    );
    expect(page).toContain("key={project.id}");
  });
});
