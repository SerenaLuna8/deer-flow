import { describe, expect, test, rs } from "@rstest/core";

import {
  createAccountQueryClient,
  transitionAccountQueries,
} from "@/core/auth/account-query-client";

describe("account query client", () => {
  test.each(["u2", null])(
    "aborts in-flight work and clears u1 cache before switching to %s",
    async (nextUserId) => {
      const client = createAccountQueryClient();
      client.setQueryData(["account", "u1", "projects"], ["private-u1"]);
      client.setQueryData(
        ["account", "u1", "project", "p1", "private-work", "threads"],
        ["thread-u1-p1"],
      );
      client.setQueryData(
        ["account", "u1", "project", "p1", "automations", "list", 50, 0],
        ["automation-u1-p1"],
      );
      let capturedSignal: AbortSignal | undefined;
      const pending = client
        .fetchQuery({
          queryKey: ["account", "u1", "project", "p1"],
          queryFn: ({ signal }) => {
            capturedSignal = signal;
            return new Promise<string>(() => undefined);
          },
        })
        .catch(() => undefined);
      await Promise.resolve();

      await transitionAccountQueries(client, "u1", nextUserId);
      expect(capturedSignal?.aborted).toBe(true);
      expect(
        client.getQueryData(["account", "u1", "projects"]),
      ).toBeUndefined();
      expect(
        client.getQueryData([
          "account",
          "u1",
          "project",
          "p1",
          "private-work",
          "threads",
        ]),
      ).toBeUndefined();
      expect(
        client.getQueryData([
          "account",
          "u1",
          "project",
          "p1",
          "automations",
          "list",
          50,
          0,
        ]),
      ).toBeUndefined();
      await pending;
    },
  );

  test("does not clear queries when the account id is unchanged", async () => {
    const client = createAccountQueryClient();
    const clear = rs.spyOn(client, "clear");
    client.setQueryData(["account", "u1"], "same-account");
    await transitionAccountQueries(client, "u1", "u1");
    expect(clear).not.toHaveBeenCalled();
    expect(client.getQueryData(["account", "u1"])).toBe("same-account");
  });

  test("force-clears a same-account cache for logout", async () => {
    const client = createAccountQueryClient();
    const clear = rs.spyOn(client, "clear");
    client.setQueryData(["account", "u1"], "logout-private");
    await transitionAccountQueries(client, "u1", "u1", { force: true });
    expect(clear).toHaveBeenCalledTimes(1);
    expect(client.getQueryData(["account", "u1"])).toBeUndefined();
  });

  test("provider clients never share cache", () => {
    const first = createAccountQueryClient();
    const second = createAccountQueryClient();
    first.setQueryData(["account", "u1"], "private");
    expect(first).not.toBe(second);
    expect(second.getQueryData(["account", "u1"])).toBeUndefined();
  });
});
