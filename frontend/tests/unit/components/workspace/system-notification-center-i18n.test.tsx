import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SystemNotificationCenter } from "@/components/workspace/system-notification-center";
import { I18nProvider } from "@/core/i18n/context";

const mutate = rs.fn();

rs.mock("@/core/system-notifications/hooks", () => ({
  useSystemNotifications: () => ({
    data: { pages: [{ items: [], unread_count: 2 }] },
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    refetch: rs.fn(),
    fetchNextPage: rs.fn(),
  }),
  useMarkAllSystemNotificationsRead: () => ({
    isPending: false,
    error: null,
    mutate,
  }),
  useAcceptSystemNotification: () => ({
    isPending: false,
    isSuccess: false,
    variables: undefined,
    error: null,
    mutate,
  }),
}));

describe("system notification locale", () => {
  test("renders the English notification trigger", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <SystemNotificationCenter userId="user-id" />
      </I18nProvider>,
    );

    expect(html).toContain('aria-label="Notifications, 2 unread items"');
    expect(html).not.toContain('aria-label="通知，2 条未读"');
  });
});
