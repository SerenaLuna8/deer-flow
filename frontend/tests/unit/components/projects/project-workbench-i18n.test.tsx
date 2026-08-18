import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectWorkbench } from "@/components/projects/project-workbench";
import { I18nProvider } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha Project",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: [...CAPABILITIES],
  is_pinned: true,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
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
  request_id: "trace",
};

const mutate = rs.fn();

rs.mock("@/core/projects/hooks", () => ({
  useProjects: () => ({
    data: { items: [project] },
    isLoading: false,
    error: null,
  }),
  useCreateProject: () => ({
    isSuccess: false,
    isPending: false,
    error: null,
    mutate,
  }),
  useUpdateProject: () => ({
    isSuccess: false,
    isPending: false,
    error: null,
    mutate,
  }),
  usePinProject: () => ({ isPending: false, mutate }),
  useRecoverableProjects: () => ({
    data: { items: [] },
    isLoading: false,
    error: null,
  }),
  useRestoreProject: () => ({
    isPending: false,
    error: null,
    mutate,
  }),
}));

rs.mock("@/components/workspace/system-notification-center", () => ({
  SystemNotificationCenter: () => (
    <button type="button" aria-label="Notifications" />
  ),
}));

describe("project workbench locale", () => {
  test("renders the workspace shell and project card in English", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <ProjectWorkbench
          userId="22222222-2222-4222-8222-222222222222"
          accountUsername="admin"
          systemRole="system_admin"
          onLogout={async () => undefined}
        />
      </I18nProvider>,
    );

    for (const text of [
      "Workspace",
      "Manage and enter your projects",
      "Search by name or project slug",
      "All projects",
      "Pinned only",
      "1 project",
      "Create project",
      "Description",
      "Actions",
      "No project description",
      "Pinned",
      "Open project",
      "Recoverable projects",
    ]) {
      expect(html).toContain(text);
    }

    expect(html).toContain('data-testid="project-list"');
    expect(html).toContain('data-testid="project-list-header"');
    expect(html).not.toContain("No projects are currently recoverable.");

    for (const label of [
      "Account",
      "Search projects",
      "Filter projects",
      "Project list",
      "Edit project",
      "Unpin project",
    ]) {
      expect(html).toContain(`aria-label="${label}"`);
    }

    expect(html).toContain('aria-pressed="true"');

    expect(enUS.projectWorkspace).toMatchObject({
      platformAdministration: "Platform administration",
      systemSettings: "System settings",
      privacyCenter: "Privacy center",
      logout: "Log out",
    });
  });
});
