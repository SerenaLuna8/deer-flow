import { afterEach, describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import { SETTINGS_SECTION_IDS } from "@/components/workspace/settings/settings-sections";
import {
  accountPersonalizationQueryKey,
  fetchAccountPersonalization,
  isAccountProjectMemoryQueryKey,
  resetAccountMemory,
  updateAccountPersonalization,
} from "@/core/account-personalization";
import {
  abortAccountPersonalizationAccount,
  runAbortableAccountPersonalizationMutation,
} from "@/core/account-personalization/api";
import { transitionAccountQueries } from "@/core/auth/account-query-client";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";

function requestURL(input: RequestInfo | URL) {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function jsonBody(init: RequestInit | undefined) {
  if (typeof init?.body !== "string") {
    throw new Error("Expected a JSON request body");
  }
  return JSON.parse(init.body) as unknown;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("account personalization client", () => {
  test("cleanup ignores development and malformed identities without weakening requests", async () => {
    expect(() => abortAccountPersonalizationAccount("default")).not.toThrow();
    expect(() =>
      abortAccountPersonalizationAccount("not-an-account"),
    ).not.toThrow();
    await expect(fetchAccountPersonalization("default")).rejects.toThrow();
  });

  test("default to UUID transition clears old caches and subsequent role loss aborts UUID work", async () => {
    const client = new QueryClient();
    client.setQueryData(
      ["account", "default", "admin", "settings", "knowledge"],
      { revision: 1 },
    );
    await expect(
      transitionAccountQueries(client, "default", ACCOUNT_ID),
    ).resolves.toBe(true);
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    client.setQueryData(accountPersonalizationQueryKey(ACCOUNT_ID), {
      version: 1,
    });
    const pending = runAbortableAccountPersonalizationMutation(
      ACCOUNT_ID,
      (signal) =>
        new Promise<never>((_resolve, reject) => {
          signal.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    const aborted = expect(pending).rejects.toMatchObject({
      name: "AbortError",
    });
    await expect(
      transitionAccountQueries(client, ACCOUNT_ID, ACCOUNT_ID, {
        previousSystemRole: "system_admin",
        nextSystemRole: "user",
      }),
    ).resolves.toBe(true);
    await aborted;
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    client.clear();
  });

  test("exposes personalization in Settings and scopes its query to the account", () => {
    expect(SETTINGS_SECTION_IDS).toContain("personalization");
    expect(accountPersonalizationQueryKey(ACCOUNT_ID)).toEqual([
      "account",
      ACCOUNT_ID,
      "personalization",
    ]);
  });

  test("loads a strict secret-free preference with the active abort signal", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          memoryEnabled: true,
          effectiveMemoryEnabled: true,
          platformMemoryAvailable: true,
          version: 1,
        }),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      fetchAccountPersonalization(ACCOUNT_ID, controller.signal),
    ).resolves.toMatchObject({ memoryEnabled: true, version: 1 });
    const [input, init] = fetcher.mock.calls[0]!;
    expect(requestURL(input)).toBe("/api/v1/account/personalization");
    expect(init?.signal).toBe(controller.signal);

    rs.stubGlobal("fetch", async () =>
      Response.json({
        memoryEnabled: true,
        effectiveMemoryEnabled: true,
        platformMemoryAvailable: true,
        version: 1,
        memoryContent: "must never enter the cache",
      }),
    );
    await expect(fetchAccountPersonalization(ACCOUNT_ID)).rejects.toThrow();
  });

  test("sends only the switch value and optimistic version", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          memoryEnabled: false,
          effectiveMemoryEnabled: false,
          platformMemoryAvailable: true,
          version: 2,
        }),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=personalization-token" });
    rs.stubGlobal("fetch", fetcher);

    await updateAccountPersonalization(ACCOUNT_ID, {
      memoryEnabled: false,
      expectedVersion: 1,
    });

    const [input, init] = fetcher.mock.calls[0]!;
    expect(requestURL(input)).toBe("/api/v1/account/personalization");
    expect(init?.method).toBe("PATCH");
    expect(jsonBody(init)).toEqual({
      memoryEnabled: false,
      expectedVersion: 1,
    });
  });

  test("requires explicit reset confirmation and sends no project or owner", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          version: 3,
          scopesReset: 2,
          historyEntries: 3,
          documents: 2,
          versions: 4,
          dreamRuns: 1,
          snapshots: 5,
          jobsCancelled: 1,
        }),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=personalization-token" });
    rs.stubGlobal("fetch", fetcher);

    await resetAccountMemory(ACCOUNT_ID, {
      confirm: true,
      expectedVersion: 2,
    });

    const [input, init] = fetcher.mock.calls[0]!;
    expect(requestURL(input)).toBe(
      "/api/v1/account/personalization/memory/reset",
    );
    expect(init?.method).toBe("POST");
    expect(jsonBody(init)).toEqual({ confirm: true, expectedVersion: 2 });
  });

  test("identifies only project Memory cache roots for the same account", () => {
    expect(
      isAccountProjectMemoryQueryKey(
        [
          "account",
          ACCOUNT_ID,
          "project",
          "22222222-2222-4222-8222-222222222222",
          "private-work",
          "memory",
          "document",
        ],
        ACCOUNT_ID,
      ),
    ).toBe(true);
    expect(
      isAccountProjectMemoryQueryKey(
        ["account", ACCOUNT_ID, "project", "p", "private-work", "threads"],
        ACCOUNT_ID,
      ),
    ).toBe(false);
    expect(
      isAccountProjectMemoryQueryKey(
        ["account", "other", "project", "p", "private-work", "memory"],
        ACCOUNT_ID,
      ),
    ).toBe(false);
  });
});
