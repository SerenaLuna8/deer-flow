import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Project description",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.update",
    "project.enter",
    "project.members.manage",
    "project.lifecycle.manage",
  ],
  is_pinned: false,
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

const membership: ProjectMembership = {
  membership_id: "22222222-2222-4222-8222-222222222222",
  user_id: "33333333-3333-4333-8333-333333333333",
  account_email: "admin@example.test",
  role: "admin",
  status: "active",
  version: 1,
  joined_at: "2026-08-22T08:00:00+08:00",
};

const mutation = {
  data: undefined,
  error: null,
  isPending: false,
  isSuccess: false,
  mutate: rs.fn(),
  reset: rs.fn(),
};
let projectReadError: Error | null = null;
let projectLifecycleError: Error | null = null;

rs.mock("next/navigation", () => ({
  usePathname: () => "/projects/alpha/settings/members",
  useRouter: () => ({ replace: rs.fn() }),
}));

rs.mock("@/components/projects/project-context", () => ({
  useCurrentProject: () => project,
}));

rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({
    user: {
      id: membership.user_id,
      email: membership.account_email,
      username: "admin",
      system_role: "user",
    },
  }),
}));

rs.mock("@/core/projects/hooks", () => ({
  useProjectMembers: () => ({
    data: [membership],
    error: projectReadError,
    isLoading: false,
  }),
  useProjectInvitations: () => ({
    data: [],
    error: null,
    isLoading: false,
  }),
  useChangeProjectMemberRole: () => mutation,
  useRemoveProjectMember: () => mutation,
  useLeaveProject: () => mutation,
  useCreateProjectInvitation: () => mutation,
  useRevokeProjectInvitation: () => mutation,
  useUpdateProject: () => mutation,
  useRequestProjectDeletion: () => ({
    ...mutation,
    error: projectLifecycleError,
  }),
}));

import { ProjectMembersPage } from "@/components/projects/members/project-members-page";
import { ProjectAccessDenied } from "@/components/projects/project-access-denied";
import { ProjectGeneralSettings } from "@/components/projects/settings/project-general-settings";
import { ProjectLifecyclePanel } from "@/components/projects/settings/project-lifecycle-panel";
import { ProjectSettingsShell } from "@/components/projects/settings/project-settings-shell";
import { I18nProvider } from "@/core/i18n/context";
import type { Project, ProjectMembership } from "@/core/projects/types";

describe("project governance locale", () => {
  test("renders the project settings shell in English", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <ProjectSettingsShell>
          <p>Settings content</p>
        </ProjectSettingsShell>
      </I18nProvider>,
    );

    expect(html).toContain("Project settings");
    expect(html).toContain("General settings");
    expect(html).toContain("Project members");
    expect(html).not.toMatch(/[\p{Script=Han}]/u);
  });

  test("renders the member and invitation surface in English", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <ProjectMembersPage embedded />
      </I18nProvider>,
    );

    expect(html).toContain("Members and invitations");
    expect(html).toContain("Invite member");
    expect(html).toContain("Leave project");
    expect(html).toContain("No project invitations.");
    expect(html).not.toMatch(/[\p{Script=Han}]/u);
  });

  test("renders project details and lifecycle settings in English", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <ProjectGeneralSettings />
        <ProjectLifecyclePanel />
      </I18nProvider>,
    );

    expect(html).toContain("Project details");
    expect(html).toContain("Project lifecycle");
    expect(html).toContain("Request project deletion");
    expect(html).not.toMatch(/[\p{Script=Han}]/u);
  });

  test("keeps access-denied and project API failures in the selected locale", () => {
    projectReadError = new Error("read failed");
    projectLifecycleError = new Error("delete failed");
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <ProjectAccessDenied projectSlug="alpha" area="project audit" />
        <ProjectMembersPage embedded />
        <ProjectLifecyclePanel />
      </I18nProvider>,
    );
    projectReadError = null;
    projectLifecycleError = null;

    expect(html).toContain("You do not have access");
    expect(html).toContain("The project request failed. Try again later.");
    expect(html).not.toMatch(/[\p{Script=Han}]/u);
  });
});
