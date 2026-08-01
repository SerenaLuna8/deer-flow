import { describe, expect, test, rs } from "@rstest/core";

import { runAbortableAdminModelMutation } from "@/core/admin-settings/models";
import {
  createAccountQueryClient,
  transitionAccountQueries,
} from "@/core/auth/account-query-client";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";

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

  test("aborts privileged work and clears cache when a same-account role changes", async () => {
    const client = createAccountQueryClient();
    const clear = rs.spyOn(client, "clear");
    let querySignal: AbortSignal | undefined;
    let mutationSignal: AbortSignal | undefined;
    client.setQueryData(
      ["account", ACCOUNT_ID, "admin", "settings", "models"],
      ["private-admin-model-config"],
    );
    const pendingQuery = client
      .fetchQuery({
        queryKey: [
          "account",
          ACCOUNT_ID,
          "admin",
          "settings",
          "models",
          "pending",
        ],
        queryFn: ({ signal }) => {
          querySignal = signal;
          return new Promise<string>(() => undefined);
        },
      })
      .catch(() => undefined);
    const pendingMutation = runAbortableAdminModelMutation(
      ACCOUNT_ID,
      (signal) =>
        new Promise<never>((_resolve, reject) => {
          mutationSignal = signal;
          signal.addEventListener(
            "abort",
            () =>
              reject(
                Object.assign(new Error("Aborted"), {
                  name: "AbortError",
                }),
              ),
            { once: true },
          );
        }),
    );
    const mutationResult = pendingMutation.catch((error: unknown) => error);
    await Promise.resolve();

    await transitionAccountQueries(client, ACCOUNT_ID, ACCOUNT_ID, {
      previousSystemRole: "system_admin",
      nextSystemRole: "user",
    });

    expect(querySignal?.aborted).toBe(true);
    expect(mutationSignal?.aborted).toBe(true);
    expect(clear).toHaveBeenCalledTimes(1);
    expect(
      client.getQueryData([
        "account",
        ACCOUNT_ID,
        "admin",
        "settings",
        "models",
      ]),
    ).toBeUndefined();
    await pendingQuery;
    await expect(mutationResult).resolves.toMatchObject({ name: "AbortError" });
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
