import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import { projectKeys } from "@/core/projects/query-keys";
import {
  commitSystemNotificationMutation,
  createSystemNotificationScope,
  SYSTEM_NOTIFICATION_POLL_INTERVAL_MS,
  systemNotificationPollInterval,
} from "@/core/system-notifications/hooks";
import { systemNotificationKeys } from "@/core/system-notifications/query-keys";

describe("system notification identity scope", () => {
  test("polls lightly only while the workspace is in the foreground", () => {
    expect(systemNotificationPollInterval("visible")).toBe(
      SYSTEM_NOTIFICATION_POLL_INTERVAL_MS,
    );
    expect(systemNotificationPollInterval("hidden")).toBe(false);
  });

  test("invalidates notifications and projects for the current account", async () => {
    const client = new QueryClient();
    const scope = createSystemNotificationScope("account-a");
    const token = scope.begin();
    const cancel = rs.spyOn(client, "cancelQueries");
    const invalidate = rs.spyOn(client, "invalidateQueries");

    await expect(
      commitSystemNotificationMutation(client, scope, token, {
        refreshProjects: true,
      }),
    ).resolves.toBe(true);

    expect(cancel).toHaveBeenCalledWith({
      queryKey: systemNotificationKeys.list("account-a"),
    });
    expect(cancel).toHaveBeenCalledWith({
      queryKey: projectKeys.workspace("account-a"),
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: systemNotificationKeys.list("account-a"),
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: projectKeys.workspace("account-a"),
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: projectKeys.myInvitations("account-a"),
    });
  });

  test("drops a late mutation after an account switch", async () => {
    const client = new QueryClient();
    const scope = createSystemNotificationScope("account-a");
    const stale = scope.begin();
    const cancel = rs.spyOn(client, "cancelQueries");
    const invalidate = rs.spyOn(client, "invalidateQueries");

    scope.update("account-b");

    await expect(
      commitSystemNotificationMutation(client, scope, stale, {
        refreshProjects: true,
      }),
    ).resolves.toBe(false);
    expect(cancel).not.toHaveBeenCalled();
    expect(invalidate).not.toHaveBeenCalled();
  });
});
