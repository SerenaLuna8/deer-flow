import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  dreamProjectMemory,
  getProjectMemory,
  getProjectMemoryVersion,
  listProjectMemoryEpisodes,
  listProjectMemoryPending,
  listProjectMemoryVersions,
  memoryDocumentSchema,
  memoryDreamResultSchema,
  memoryEpisodeSchema,
  memoryPendingEntrySchema,
  memoryVersionDetailSchema,
  memoryVersionSummarySchema,
  projectMemoryEpisodesQueryKey,
  projectMemoryPendingQueryKey,
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
  needsReview: false,
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
          injectionStatus: "ok",
        }),
    );
    rs.stubGlobal("fetch", fetcher);

    const result = await getProjectMemory(access, controller.signal);

    expect(result.pendingCount).toBe(4);
    expect(result.injectionStatus).toBe("ok");
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

  test("accepts the explicit zero-history budget rewrite admission", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(
          {
            disposition: "queued",
            historyCount: 0,
            jobId: JOB_ID,
            admissionKind: "budget_rewrite",
          },
          { status: 202 },
        ),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=memory-token" });
    rs.stubGlobal("fetch", fetcher);

    const result = await dreamProjectMemory(access);

    expect(result).toEqual({
      disposition: "queued",
      historyCount: 0,
      jobId: JOB_ID,
      admissionKind: "budget_rewrite",
    });
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
        injectionStatus: "ok",
        ownerUserId: ACCOUNT_ID,
      }).success,
    ).toBe(false);
    expect(
      memoryDocumentSchema.safeParse({
        content: "ok",
        version: 1,
        updatedAt: TIMESTAMP,
        pendingCount: 0,
        dreamRunning: false,
        injectionStatus: "partial",
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
    expect(
      memoryDreamResultSchema.safeParse({
        disposition: "nothing_pending",
        jobId: null,
        historyCount: 0,
      }).success,
    ).toBe(true);
    expect(
      memoryDreamResultSchema.safeParse({
        disposition: "nothing_pending",
        jobId: null,
        historyCount: 0,
        admissionKind: "budget_rewrite",
      }).success,
    ).toBe(false);
    expect(
      memoryDreamResultSchema.safeParse({
        disposition: "queued",
        jobId: JOB_ID,
        historyCount: 0,
      }).success,
    ).toBe(false);

    expect(
      memoryVersionSummarySchema.safeParse({
        ...version,
        trigger: "budget_rewrite",
        historyCount: 0,
      }).success,
    ).toBe(true);
    expect(
      memoryVersionSummarySchema.safeParse({
        ...version,
        historyCount: 0,
      }).success,
    ).toBe(false);
    expect(
      memoryVersionSummarySchema.safeParse({
        ...version,
        trigger: "restore",
        historyCount: null,
      }).success,
    ).toBe(true);

    rs.stubGlobal("fetch", async () =>
      Response.json({
        content: "ok",
        version: 1,
        updatedAt: TIMESTAMP,
        pendingCount: 0,
        dreamRunning: false,
        injectionStatus: "ok",
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

  test("searches the archive with exact filters and scoped keys", async () => {
    const controller = new AbortController();
    const episode = {
      id: JOB_ID,
      threadId: "thread-1",
      origin: "snip",
      taggedText: "- [durable] deployment target is region-eu",
      occurredAt: TIMESTAMP,
      createdAt: TIMESTAMP,
    };
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({ items: [episode] }),
    );
    rs.stubGlobal("fetch", fetcher);

    const result = await listProjectMemoryEpisodes(
      access,
      {
        q: "deployment",
        tags: ["durable", "permanent"],
        before: TIMESTAMP,
        limit: 20,
      },
      controller.signal,
    );

    expect(result.items).toEqual([episode]);
    const [input, init] = fetcher.mock.calls[0]!;
    const url = new URL(requestURL(input), "http://local.test");
    expect(url.pathname).toBe(`/api/projects/${PROJECT_ID}/memory/episodes`);
    expect(url.searchParams.get("q")).toBe("deployment");
    expect(url.searchParams.getAll("tags")).toEqual(["durable", "permanent"]);
    expect(url.searchParams.get("before")).toBe(TIMESTAMP);
    expect(url.searchParams.get("limit")).toBe("20");
    expect(init?.signal).toBe(controller.signal);
    expect(
      projectMemoryEpisodesQueryKey(access.scope, {
        q: "deployment",
        tags: ["permanent", "durable"],
      }),
    ).toEqual(
      expect.arrayContaining([
        ACCOUNT_ID,
        PROJECT_ID,
        "memory",
        "episodes",
        "deployment",
        "durable,permanent",
      ]),
    );
  });

  test("rejects out-of-contract archive inputs and responses", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({ items: [] }),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      listProjectMemoryEpisodes(access, { q: "x".repeat(201), limit: 20 }),
    ).rejects.toThrow();
    await expect(
      listProjectMemoryEpisodes(access, {
        tags: ["skip" as never],
        limit: 20,
      }),
    ).rejects.toThrow();
    await expect(
      listProjectMemoryEpisodes(access, { limit: 51 }),
    ).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();

    expect(
      memoryEpisodeSchema.safeParse({
        id: JOB_ID,
        threadId: "thread-1",
        origin: "manual",
        taggedText: "- [durable] x",
        occurredAt: TIMESTAMP,
        createdAt: TIMESTAMP,
      }).success,
    ).toBe(false);
    expect(
      memoryEpisodeSchema.safeParse({
        id: JOB_ID,
        threadId: "thread-1",
        origin: "snip",
        taggedText: "- [durable] x",
        occurredAt: TIMESTAMP,
        createdAt: TIMESTAMP,
        ownerUserId: ACCOUNT_ID,
      }).success,
    ).toBe(false);
  });

  test("reads the pending backlog with bounded pagination and scoped keys", async () => {
    const controller = new AbortController();
    const entry = {
      sequence: 41,
      origin: "tool",
      taggedText: "- [durable] deployment target is region-eu",
      createdAt: TIMESTAMP,
    };
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({ items: [entry] }),
    );
    rs.stubGlobal("fetch", fetcher);

    const result = await listProjectMemoryPending(
      access,
      {},
      controller.signal,
    );

    expect(result.items).toEqual([entry]);
    const [input, init] = fetcher.mock.calls[0]!;
    const url = new URL(requestURL(input), "http://local.test");
    expect(url.pathname).toBe(`/api/projects/${PROJECT_ID}/memory/pending`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: "50",
      offset: "0",
    });
    expect(init?.signal).toBe(controller.signal);
    expect(projectMemoryPendingQueryKey(access.scope)).toEqual(
      expect.arrayContaining([ACCOUNT_ID, PROJECT_ID, "memory", "pending"]),
    );
  });

  test("rejects out-of-contract pending inputs and responses", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({ items: [] }),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      listProjectMemoryPending(access, { limit: 101 }),
    ).rejects.toThrow();
    await expect(
      listProjectMemoryPending(access, { offset: -1 }),
    ).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();

    expect(
      memoryPendingEntrySchema.safeParse({
        sequence: 41,
        origin: "manual",
        taggedText: "- [durable] x",
        createdAt: TIMESTAMP,
      }).success,
    ).toBe(false);
    expect(
      memoryPendingEntrySchema.safeParse({
        sequence: 41,
        origin: "tool",
        taggedText: "- [durable] x",
        createdAt: TIMESTAMP,
        ownerUserId: ACCOUNT_ID,
      }).success,
    ).toBe(false);
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
