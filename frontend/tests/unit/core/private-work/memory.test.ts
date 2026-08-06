import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  dreamProjectMemory,
  getProjectMemory,
  getProjectMemoryVersion,
  listProjectMemoryVersions,
  memoryDocumentSchema,
  memoryDreamResultSchema,
  memoryVersionDetailSchema,
  projectMemoryPermissions,
  projectMemoryVersionsQueryKey,
  restoreProjectMemoryVersion,
} from "@/core/private-work/memory";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const JOB_ID = "33333333-3333-4333-8333-333333333333";
const TIMESTAMP = "2026-08-05T00:00:00Z";

const access = {
  apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
};

const version = {
  version: 3,
  trigger: "manual_dream",
  historyCount: 2,
  changed: true,
  createdAt: TIMESTAMP,
};

function requestURL(input: RequestInfo | URL) {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function jsonBody(init: RequestInit | undefined) {
  if (typeof init?.body !== "string") {
    throw new Error("Expected a JSON string request body");
  }
  return JSON.parse(init.body) as unknown;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("project Memory document client", () => {
  test("reads the current document without client-selected authority fields", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          content: "# About me\n\nPrefers concise plans.",
          version: 3,
          updatedAt: TIMESTAMP,
          pendingCount: 4,
          dreamRunning: false,
        }),
    );
    rs.stubGlobal("fetch", fetcher);

    const result = await getProjectMemory(access, controller.signal);

    expect(result.pendingCount).toBe(4);
    const [input, init] = fetcher.mock.calls[0]!;
    const url = new URL(requestURL(input), "http://local.test");
    expect(url.pathname).toBe(`/api/projects/${PROJECT_ID}/memory`);
    expect(url.search).toBe("");
    expect(init?.signal).toBe(controller.signal);
  });

  test("passes only the current thread when Dream is admitted", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(
          { disposition: "queued", historyCount: 2, jobId: JOB_ID },
          { status: 202 },
        ),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=memory-token" });
    rs.stubGlobal("fetch", fetcher);

    const result = await dreamProjectMemory(access, { threadId: "thread-1" });

    expect(result.disposition).toBe("queued");
    const [input, init] = fetcher.mock.calls[0]!;
    const url = new URL(requestURL(input), "http://local.test");
    expect(url.pathname).toBe(`/api/projects/${PROJECT_ID}/memory/dream`);
    expect(url.search).toBe("");
    expect(init?.method).toBe("POST");
    expect(jsonBody(init)).toEqual({ threadId: "thread-1" });
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("memory-token");
  });

  test("uses bounded version pagination and scoped query keys", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({ items: [version] }),
    );
    rs.stubGlobal("fetch", fetcher);

    const result = await listProjectMemoryVersions(access, {
      limit: 51,
      offset: 50,
    });

    expect(result.items).toHaveLength(1);
    const [input] = fetcher.mock.calls[0]!;
    const url = new URL(requestURL(input), "http://local.test");
    expect(url.pathname).toBe(`/api/projects/${PROJECT_ID}/memory/versions`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: "51",
      offset: "50",
    });
    expect(
      projectMemoryVersionsQueryKey(access.scope, { limit: 51, offset: 50 }),
    ).toEqual(
      expect.arrayContaining([ACCOUNT_ID, PROJECT_ID, "memory", "versions"]),
    );
  });

  test("reads a real diff and restores through the exact version route", async () => {
    const detail = {
      ...version,
      content: "# About me\n\nPrefers concise plans.",
      unifiedDiff: "@@ -1 +1 @@\n-old\n+new",
    };
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(detail),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=memory-token" });
    rs.stubGlobal("fetch", fetcher);

    const loaded = await getProjectMemoryVersion(access, 3);
    const restored = await restoreProjectMemoryVersion(access, 3, {
      expectedCurrentVersion: 4,
    });

    expect(loaded.unifiedDiff).toContain("+new");
    expect(restored.version).toBe(3);
    const [readInput, readInit] = fetcher.mock.calls[0]!;
    const [restoreInput, restoreInit] = fetcher.mock.calls[1]!;
    expect(new URL(requestURL(readInput), "http://local.test").pathname).toBe(
      `/api/projects/${PROJECT_ID}/memory/versions/3`,
    );
    expect(readInit?.method).toBeUndefined();
    expect(
      new URL(requestURL(restoreInput), "http://local.test").pathname,
    ).toBe(`/api/projects/${PROJECT_ID}/memory/versions/3/restore`);
    expect(restoreInit?.method).toBe("POST");
    expect(jsonBody(restoreInit)).toEqual({ expectedCurrentVersion: 4 });
  });

  test("rejects malformed or authority-widening response fields", async () => {
    expect(
      memoryDocumentSchema.safeParse({
        content: "ok",
        version: 1,
        updatedAt: TIMESTAMP,
        pendingCount: 0,
        dreamRunning: false,
        ownerUserId: ACCOUNT_ID,
      }).success,
    ).toBe(false);
    expect(
      memoryVersionDetailSchema.safeParse({
        ...version,
        content: "ok",
        unifiedDiff: "x".repeat(64_001),
      }).success,
    ).toBe(false);
    expect(
      memoryDreamResultSchema.safeParse({
        disposition: "nothing_pending",
        jobId: JOB_ID,
        historyCount: 0,
      }).success,
    ).toBe(false);

    rs.stubGlobal("fetch", async () =>
      Response.json({
        content: "ok",
        version: 1,
        updatedAt: TIMESTAMP,
        pendingCount: 0,
        dreamRunning: false,
        namespace: "default",
      }),
    );
    await expect(getProjectMemory(access)).rejects.toThrow();
  });

  test("validates version, thread and pagination inputs before fetch", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({}),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(getProjectMemoryVersion(access, 0)).rejects.toThrow();
    await expect(
      dreamProjectMemory(access, { threadId: "x".repeat(65) }),
    ).rejects.toThrow();
    await expect(
      listProjectMemoryVersions(access, { limit: 101, offset: 0 }),
    ).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });

  test("maps capabilities without inferring roles", () => {
    expect(projectMemoryPermissions(["private_work.read_own"])).toEqual({
      canRead: true,
      canDream: false,
      canRestore: false,
    });
    expect(projectMemoryPermissions(["private_work.create"])).toEqual({
      canRead: false,
      canDream: true,
      canRestore: true,
    });
  });
});
