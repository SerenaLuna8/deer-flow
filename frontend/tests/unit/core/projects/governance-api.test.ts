import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  changeProjectMemberRole,
  claimProjectInvitation,
  createProjectInvitation,
  leaveProject,
  listMyProjectInvitations,
  listProjectInvitations,
  listProjectMembers,
  listProjects,
  redeemProjectInvitation,
  removeProjectMember,
  requestProjectDeletion,
  restoreProject,
  revokeProjectInvitation,
} from "@/core/projects/api";

const mockedFetch = rs.mocked(fetchWithAuth);
const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const MEMBERSHIP_ID = "22222222-2222-4222-8222-222222222222";
const INVITATION_ID = "33333333-3333-4333-8333-333333333333";

const member = {
  membership_id: MEMBERSHIP_ID,
  user_id: "44444444-4444-4444-8444-444444444444",
  account_email: "member@example.com",
  role: "editor",
  status: "active",
  version: 3,
  joined_at: "2026-07-12T08:00:00+00:00",
};

const invitation = {
  id: INVITATION_ID,
  project_id: PROJECT_ID,
  invited_email: "invitee@example.com",
  role: "viewer",
  status: "pending",
  expires_at: "2026-07-19T08:00:00+00:00",
  version: 2,
  created_at: "2026-07-12T08:00:00+00:00",
};

const project = {
  id: PROJECT_ID,
  slug: "alpha-project",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: ["project.read", "project.lifecycle.manage"],
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
  deletion_effective_at: null,
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project governance api", () => {
  test("uses scoped membership routes and versioned mutation bodies", async () => {
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(200, [member]))
      .mockImplementation(() => Promise.resolve(jsonResponse(200, member)));
    const signal = new AbortController().signal;

    await listProjectMembers(PROJECT_ID, signal);
    await changeProjectMemberRole(
      PROJECT_ID,
      MEMBERSHIP_ID,
      { role: "admin", version: 3 },
      signal,
    );
    await removeProjectMember(PROJECT_ID, MEMBERSHIP_ID, 3, signal);
    await leaveProject(PROJECT_ID, 7, signal);

    expect(mockedFetch.mock.calls).toEqual([
      [`/backend/api/projects/${PROJECT_ID}/members`, { signal }],
      [
        `/backend/api/projects/${PROJECT_ID}/members/${MEMBERSHIP_ID}`,
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ role: "admin", version: 3 }),
          signal,
        }),
      ],
      [
        `/backend/api/projects/${PROJECT_ID}/members/${MEMBERSHIP_ID}`,
        expect.objectContaining({
          method: "DELETE",
          body: JSON.stringify({ version: 3 }),
          signal,
        }),
      ],
      [
        `/backend/api/projects/${PROJECT_ID}/leave`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ version: 7 }),
          signal,
        }),
      ],
    ]);
  });

  test("uses invitation contracts and sends the secret only in claim", async () => {
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(200, [invitation]))
      .mockResolvedValueOnce(jsonResponse(200, [invitation]))
      .mockResolvedValueOnce(
        jsonResponse(201, {
          ...invitation,
          invite_url_fragment: "/invite#token=plain-secret",
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, invitation))
      .mockResolvedValueOnce(
        jsonResponse(200, { message: "Invitation claim processed" }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          invitation_id: INVITATION_ID,
          project_id: PROJECT_ID,
          project_slug: "alpha-project",
          membership_id: MEMBERSHIP_ID,
          role: "viewer",
        }),
      );
    const signal = new AbortController().signal;

    await listMyProjectInvitations(signal);
    await listProjectInvitations(PROJECT_ID, signal);
    await createProjectInvitation(
      PROJECT_ID,
      { email: "invitee@example.com", role: "viewer" },
      signal,
    );
    await revokeProjectInvitation(PROJECT_ID, INVITATION_ID, 2, signal);
    await claimProjectInvitation("plain-secret", signal);
    await redeemProjectInvitation(signal);

    expect(mockedFetch.mock.calls).toEqual([
      ["/backend/api/project-invitations/mine", { signal }],
      [`/backend/api/projects/${PROJECT_ID}/invitations`, { signal }],
      [
        `/backend/api/projects/${PROJECT_ID}/invitations`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            email: "invitee@example.com",
            role: "viewer",
          }),
          signal,
        }),
      ],
      [
        `/backend/api/projects/${PROJECT_ID}/invitations/${INVITATION_ID}`,
        expect.objectContaining({
          method: "DELETE",
          body: JSON.stringify({ version: 2 }),
          signal,
        }),
      ],
      [
        "/backend/api/project-invitations/claim",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ token: "plain-secret" }),
          signal,
        }),
      ],
      [
        "/backend/api/project-invitations/redeem",
        expect.objectContaining({ method: "POST", signal }),
      ],
    ]);
    expect(mockedFetch.mock.calls[5]?.[1]).not.toHaveProperty("body");
    expect(mockedFetch.mock.calls[5]?.[0]).not.toContain("plain-secret");
  });

  test("lists recoverable projects separately and supports lifecycle mutations", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [
            {
              ...project,
              status: "pending_deletion",
              deletion_effective_at: "2026-08-11T08:00:00+00:00",
            },
          ],
          next_cursor: null,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          ...project,
          status: "pending_deletion",
          deletion_effective_at: "2026-08-11T08:00:00+00:00",
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, project));
    const signal = new AbortController().signal;

    const recoverable = await listProjects(
      { includeRecoverable: true },
      signal,
    );
    await requestProjectDeletion(PROJECT_ID, signal);
    await restoreProject(PROJECT_ID, signal);

    expect(recoverable.items[0]?.status).toBe("pending_deletion");
    expect(mockedFetch.mock.calls).toEqual([
      ["/backend/api/projects?include_recoverable=true", { signal }],
      [
        `/backend/api/projects/${PROJECT_ID}/deletion`,
        expect.objectContaining({ method: "POST", signal }),
      ],
      [
        `/backend/api/projects/${PROJECT_ID}/restore`,
        expect.objectContaining({ method: "POST", signal }),
      ],
    ]);
  });

  test("accepts stable governance errors with request ids and ignores private messages", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          code: "PROJECT_LAST_ADMIN",
          message: "private membership and email detail",
          request_id: "request-safe-1",
        },
      }),
    );

    await expect(leaveProject(PROJECT_ID, 1)).rejects.toMatchObject({
      status: 409,
      code: "PROJECT_LAST_ADMIN",
      message: "Project must keep an active admin",
    });
  });

  test("strictly rejects token material in ordinary invitation responses", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, [{ ...invitation, token_hash: "should-not-arrive" }]),
    );

    await expect(listMyProjectInvitations()).rejects.toMatchObject({
      code: "PROJECT_RESPONSE_INVALID",
      message: "Project response was invalid",
    });
  });
});
