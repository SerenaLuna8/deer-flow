import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/system-notifications/hooks", () => ({
  useSystemNotifications: () => ({
    data: {
      pages: [
        {
          items: [
            {
              id: "11111111-1111-4111-8111-111111111111",
              kind: "project_invitation",
              project: {
                id: "22222222-2222-4222-8222-222222222222",
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
            },
          ],
          next_cursor: null,
          unread_count: 1,
        },
      ],
      pageParams: [null],
    },
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: rs.fn(),
    isLoading: false,
    error: null,
    refetch: rs.fn(),
  }),
  useMarkAllSystemNotificationsRead: () => ({
    mutate: rs.fn(),
    error: null,
    isPending: false,
  }),
  useAcceptSystemNotification: () => ({
    mutate: rs.fn(),
    error: null,
    isPending: false,
    isSuccess: false,
    variables: undefined,
  }),
}));

import { SystemNotificationCenter } from "@/components/workspace/system-notification-center";

describe("system notification center", () => {
  test("announces the account-scoped unread count from the workspace header", () => {
    const html = renderToStaticMarkup(
      <SystemNotificationCenter userId="account-a" />,
    );

    expect(html).toContain('aria-label="通知，1 条未读"');
    expect(html).toContain(">1<");
  });
});
