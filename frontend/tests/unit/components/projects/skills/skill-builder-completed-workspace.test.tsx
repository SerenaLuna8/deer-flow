import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const THREAD_ID = "44444444-4444-4444-8444-444444444444";
const SKILL_ID = "55555555-5555-4555-8555-555555555555";
const BASE_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const CANDIDATE_VERSION_ID = "77777777-7777-4777-8777-777777777777";
const NOW = "2026-08-22T08:00:00Z";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Completed Skill Builder regression fixture",
  icon: "folder",
  role: "editor",
  capabilities: [
    "project.read",
    "project.enter",
    "shared_assets.read",
    "shared_assets.edit",
  ],
  is_pinned: false,
  created_at: "2026-07-01T00:00:00Z",
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 1,
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
  request_id: "request-alpha",
};
let currentProject = project;

rs.mock("next/navigation", () => ({
  usePathname: () => `/projects/alpha/skills/new/${SESSION_ID}`,
  useRouter: () => ({
    push: rs.fn(),
    replace: rs.fn(),
    refresh: rs.fn(),
  }),
}));

rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({
    user: {
      id: ACCOUNT_ID,
      email: "owner@example.test",
      username: "owner",
      system_role: "user",
      needs_setup: false,
      oauth_provider: null,
    },
  }),
}));

rs.mock("@/components/projects/project-context", () => ({
  useCurrentProject: () => currentProject,
}));

import {
  SkillBuilderWorkspace,
  skillBuilderShouldRestoreConversationSurface,
} from "@/components/projects/skills/skill-builder-workspace";
import { I18nProvider } from "@/core/i18n/context";
import { modelsQueryKey } from "@/core/models/hooks";
import { PrivateWorkProvider } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";
import {
  skillBuilderActivitiesKey,
  skillBuilderSessionKey,
  type SkillBuilderSession,
} from "@/core/skill-builder";

const completedRevision: SkillBuilderSession = {
  id: SESSION_ID,
  project_id: PROJECT_ID,
  owner_user_id: ACCOUNT_ID,
  thread_id: THREAD_ID,
  slug: "catalog-auditor",
  display_name: "Catalog auditor",
  status: "completed",
  revision: 8,
  messages: [],
  active_clarification: null,
  progress: [],
  files: [],
  draft_checksum: "a".repeat(64),
  validation: null,
  error_code: null,
  error_message: null,
  created_skill_id: SKILL_ID,
  created_skill_version_id: CANDIDATE_VERSION_ID,
  session_kind: "revise",
  target_skill_id: SKILL_ID,
  base_version_id: BASE_VERSION_ID,
  base_version_number: 3,
  base_payload_checksum: "b".repeat(64),
  target_skill_deleted: false,
  base_files: [],
  created_at: NOW,
  updated_at: NOW,
};

const completedCreation: SkillBuilderSession = {
  ...completedRevision,
  slug: "new-catalog-auditor",
  display_name: "New catalog auditor",
  session_kind: "create",
  target_skill_id: null,
  base_version_id: null,
  base_version_number: null,
  base_payload_checksum: null,
};

function renderCompletedSession(
  session: SkillBuilderSession = completedRevision,
): string {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  queryClient.setQueryData(
    skillBuilderSessionKey(ACCOUNT_ID, PROJECT_ID, SESSION_ID),
    session,
  );
  queryClient.setQueryData(
    skillBuilderActivitiesKey(ACCOUNT_ID, PROJECT_ID, SESSION_ID),
    [],
  );
  queryClient.setQueryData(modelsQueryKey, {
    models: [],
    token_usage: { enabled: false },
  });

  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <PrivateWorkProvider
        access={{
          scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
          client: {} as never,
          apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
          queryKeyPrefix: ["completed-skill-builder"],
          reconnectOnMount: false,
          isActive: () => true,
        }}
      >
        <I18nProvider initialLocale="en-US">
          <SkillBuilderWorkspace sessionId={SESSION_ID} />
        </I18nProvider>
      </PrivateWorkProvider>
    </QueryClientProvider>,
  );
}

function workspaceHeader(html: string): string {
  const header = /<header\b[\s\S]*?<\/header>/u.exec(html)?.[0];
  if (!header)
    throw new Error("Skill Builder workspace header was not rendered");
  return header;
}

describe("completed Skill Builder workspace", () => {
  beforeEach(() => {
    currentProject = project;
  });

  test("hides the destructive Abandon menu in the read-only record", () => {
    const header = workspaceHeader(renderCompletedSession());

    expect(header).not.toContain('aria-label="More actions"');
  });

  test("restores the conversation surface when completion clears candidate files", () => {
    expect(
      skillBuilderShouldRestoreConversationSurface({
        status: "completed",
        files: [],
      }),
    ).toBe(true);
    expect(
      skillBuilderShouldRestoreConversationSurface({
        status: "draft_ready",
        files: [],
      }),
    ).toBe(false);
  });

  test("uses compact Candidate Version and read-only copy in the revision header", () => {
    const header = workspaceHeader(renderCompletedSession());

    expect(header).not.toContain("Revising catalog-auditor v3");
    expect(header).toContain("Candidate Version");
    expect(header).toContain("Read-only");
    expect(header).not.toContain("saved");
    expect(header).not.toContain("design record");
  });

  test("uses the same completed read-only copy for a created Skill", () => {
    const header = workspaceHeader(renderCompletedSession(completedCreation));

    expect(header).not.toContain("Automatically saved; continue later");
    expect(header).toContain("Candidate Version");
    expect(header).toContain("Read-only");
    expect(header).not.toContain("saved");
    expect(header).not.toContain("design record");
    expect(renderCompletedSession(completedCreation)).not.toContain(
      "Review and activate it when ready",
    );
  });

  test("does not misreport a completed reader-owned record as a permission failure", () => {
    currentProject = {
      ...project,
      role: "viewer",
      capabilities: [
        "project.read",
        "project.enter",
        "project.pin",
        "shared_assets.read",
      ],
    };
    const permissionFailure =
      "Your account cannot continue revising this Skill. Saved session content and candidate files remain available to view.";

    expect(renderCompletedSession()).not.toContain(permissionFailure);
    expect(
      renderCompletedSession({
        ...completedRevision,
        status: "draft_ready",
        created_skill_id: null,
        created_skill_version_id: null,
      }),
    ).toContain(permissionFailure);
  });
});
