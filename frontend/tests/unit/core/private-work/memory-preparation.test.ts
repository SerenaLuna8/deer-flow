import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  admitProjectMemoryDreamPreparation,
  cancelProjectMemoryDreamPreparation,
  getLatestProjectMemoryDreamPreparation,
  getProjectMemoryDreamPreparation,
} from "@/core/private-work/memory/api";
import {
  projectMemoryDreamPreparationQueryKey,
  projectMemoryLatestDreamPreparationQueryKey,
} from "@/core/private-work/memory/query-keys";
import {
  memoryDreamPreparationAdmissionSchema,
  memoryDreamPreparationStatusSchema,
} from "@/core/private-work/memory/schemas";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const JOB_ID = "33333333-3333-4333-8333-333333333333";
const DREAM_JOB_ID = "44444444-4444-4444-8444-444444444444";
const OPERATION_ID = "55555555-5555-4555-8555-555555555555";

const access = {
  apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
};

const runningStatus = {
  jobId: JOB_ID,
  status: "running",
  phase: "draining",
  compactedPasses: 3,
  dreamJobId: null,
  historyCount: null,
  admissionKind: null,
  resultDisposition: "queued",
  cancelRequested: false,
  publicErrorCode: null,
  updatedAt: "2026-08-13T00:00:00Z",
} as const;

function requestURL(input: RequestInfo | URL) {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function jsonBody(init: RequestInit | undefined) {
  if (typeof init?.body !== "string") throw new Error("Expected JSON body");
  return JSON.parse(init.body) as unknown;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("project Memory Dream preparation client", () => {
  test("admits one durable preparation with a stable operation identity", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(
          { disposition: "queued", jobId: JOB_ID, status: "queued" },
          { status: 202 },
        ),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=prepare-token" });
    rs.stubGlobal("fetch", fetcher);

    const result = await admitProjectMemoryDreamPreparation(
      access,
      { threadId: "thread-1", operationId: OPERATION_ID },
      controller.signal,
    );

    expect(result).toEqual({
      disposition: "queued",
      jobId: JOB_ID,
      status: "queued",
    });
    const [input, init] = fetcher.mock.calls[0]!;
    expect(new URL(requestURL(input), "http://local.test").pathname).toBe(
      `/api/projects/${PROJECT_ID}/memory/dream-preparations`,
    );
    expect(init?.method).toBe("POST");
    expect(init?.signal).toBe(controller.signal);
    expect(jsonBody(init)).toEqual({
      threadId: "thread-1",
      operationId: OPERATION_ID,
    });
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
      "prepare-token",
    );
  });

  test("reads exact and latest status and forwards abort signals", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(runningStatus),
    );
    rs.stubGlobal("fetch", fetcher);

    const exact = await getProjectMemoryDreamPreparation(
      access,
      JOB_ID,
      controller.signal,
    );
    const latest = await getLatestProjectMemoryDreamPreparation(
      access,
      "thread/with space",
      controller.signal,
    );

    expect(exact.compactedPasses).toBe(3);
    expect(latest.status).toBe("running");
    const exactURL = new URL(
      requestURL(fetcher.mock.calls[0]![0]),
      "http://local.test",
    );
    const latestURL = new URL(
      requestURL(fetcher.mock.calls[1]![0]),
      "http://local.test",
    );
    expect(exactURL.pathname).toBe(
      `/api/projects/${PROJECT_ID}/memory/dream-preparations/${JOB_ID}`,
    );
    expect(latestURL.pathname).toBe(
      `/api/projects/${PROJECT_ID}/memory/dream-preparations/latest`,
    );
    expect(latestURL.searchParams.get("threadId")).toBe("thread/with space");
    expect(fetcher.mock.calls[0]![1]?.signal).toBe(controller.signal);
    expect(fetcher.mock.calls[1]![1]?.signal).toBe(controller.signal);
  });

  test("requests cooperative cancellation and parses the returned state", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({ ...runningStatus, cancelRequested: true }),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=prepare-token" });
    rs.stubGlobal("fetch", fetcher);

    const result = await cancelProjectMemoryDreamPreparation(
      access,
      JOB_ID,
      controller.signal,
    );

    expect(result.cancelRequested).toBe(true);
    const [input, init] = fetcher.mock.calls[0]!;
    expect(new URL(requestURL(input), "http://local.test").pathname).toBe(
      `/api/projects/${PROJECT_ID}/memory/dream-preparations/${JOB_ID}/cancel`,
    );
    expect(init?.method).toBe("POST");
    expect(init?.signal).toBe(controller.signal);
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
      "prepare-token",
    );
  });

  test("keeps responses strict and content-free", () => {
    expect(
      memoryDreamPreparationAdmissionSchema.safeParse({
        disposition: "queued",
        jobId: JOB_ID,
        status: "queued",
        ownerUserId: ACCOUNT_ID,
      }).success,
    ).toBe(false);
    expect(
      memoryDreamPreparationStatusSchema.safeParse({
        ...runningStatus,
        checkpointId: "private-checkpoint",
      }).success,
    ).toBe(false);
    expect(
      memoryDreamPreparationStatusSchema.safeParse({
        ...runningStatus,
        publicErrorCode: "raw provider error",
      }).success,
    ).toBe(false);
    expect(
      memoryDreamPreparationStatusSchema.safeParse({
        ...runningStatus,
        status: "succeeded",
        phase: "succeeded",
        dreamJobId: DREAM_JOB_ID,
        historyCount: 2,
        admissionKind: "history",
      }).success,
    ).toBe(true);
  });

  test("keeps status keys account/project rooted and rejects malformed coordinates", () => {
    expect(projectMemoryDreamPreparationQueryKey(access.scope, JOB_ID)).toEqual(
      [
        "account",
        ACCOUNT_ID,
        "project",
        PROJECT_ID,
        "private-work",
        "memory",
        "dream-preparation",
        JOB_ID,
      ],
    );
    expect(
      projectMemoryLatestDreamPreparationQueryKey(access.scope, "thread-1"),
    ).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "memory",
      "dream-preparation",
      "latest",
      "thread-1",
    ]);
    expect(() =>
      projectMemoryDreamPreparationQueryKey(access.scope, "not-a-uuid"),
    ).toThrow();
    expect(() =>
      projectMemoryLatestDreamPreparationQueryKey(access.scope, ""),
    ).toThrow();
  });
});
