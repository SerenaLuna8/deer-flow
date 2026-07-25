import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  acceptSystemNotification,
  listSystemNotifications,
  markAllSystemNotificationsRead,
} from "@/core/system-notifications/api";
import { systemNotificationKeys } from "@/core/system-notifications/query-keys";
import { systemNotificationPageSchema } from "@/core/system-notifications/types";

const mockedFetch = rs.mocked(fetchWithAuth);
const NOTIFICATION_ID = "11111111-1111-4111-8111-111111111111";
const INVITATION_ID = "44444444-4444-4444-8444-444444444444";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";

const notification = {
  id: NOTIFICATION_ID,
  kind: "project_invitation",
  project: {
    id: PROJECT_ID,
    slug: "research-lab",
    display_name: "Research Lab",
  },
  actor: { email: "owner@example.com" },
  role: "viewer",
  status: "pending",
  is_read: false,
  created_at: "2026-07-25T08:00:00+00:00",
  expires_at: "2026-08-01T08:00:00+00:00",
  version: 2,
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

describe("system notifications", () => {
  test("uses account-isolated query keys", () => {
    expect(systemNotificationKeys.list("account-a")).toEqual([
      "account",
      "account-a",
      "system-notifications",
    ]);
    expect(systemNotificationKeys.list("account-a")).not.toEqual(
      systemNotificationKeys.list("account-b"),
    );
  });

  test("lists, marks read, and accepts invitations through safe routes", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [notification],
          next_cursor: null,
          unread_count: 1,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, { marked_count: 1 }))
      .mockResolvedValueOnce(
        jsonResponse(200, {
          invitation_id: INVITATION_ID,
          project_id: PROJECT_ID,
          project_slug: "research-lab",
          membership_id: "33333333-3333-4333-8333-333333333333",
          role: "viewer",
        }),
      );
    const signal = new AbortController().signal;

    await listSystemNotifications({ limit: 50 }, signal);
    await markAllSystemNotificationsRead(signal);
    await acceptSystemNotification(NOTIFICATION_ID, 2, signal);

    expect(mockedFetch.mock.calls).toEqual([
      ["/backend/api/notifications?limit=50", { signal }],
      [
        "/backend/api/notifications/read-all",
        expect.objectContaining({ method: "POST", signal }),
      ],
      [
        `/backend/api/notifications/${NOTIFICATION_ID}/accept`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ version: 2 }),
          signal,
        }),
      ],
    ]);
  });

  test("loads later notification pages with an opaque cursor", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        items: [],
        next_cursor: null,
        unread_count: 0,
      }),
    );
    const signal = new AbortController().signal;

    await listSystemNotifications(
      { cursor: "opaque cursor/+=", limit: 50 },
      signal,
    );

    expect(mockedFetch).toHaveBeenCalledWith(
      "/backend/api/notifications?cursor=opaque+cursor%2F%2B%3D&limit=50",
      { signal },
    );
  });

  test("rejects unknown authority fields from notification responses", () => {
    expect(
      systemNotificationPageSchema.safeParse({
        items: [{ ...notification, invited_email: "secret@example.com" }],
        next_cursor: null,
        unread_count: 1,
      }).success,
    ).toBe(false);
  });
});
