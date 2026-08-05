import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  acceptProjectMemoryV2Candidate,
  consolidateProjectMemoryV2,
  disableProjectMemoryV2Fact,
  exportProjectMemoryV2,
  getProjectMemoryV2Status,
  hardForgetProjectMemoryV2Fact,
  listProjectMemoryV2Facts,
  memoryV2CandidateSchema,
  memoryV2EvidenceSchema,
  memoryV2RevisionSchema,
  projectMemoryV2FactsQueryKey,
  projectMemoryV2Permissions,
  reviseProjectMemoryV2Fact,
} from "@/core/private-work/memory";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const FACT_ID = "33333333-3333-4333-8333-333333333333";
const REVISION_ID = "44444444-4444-4444-8444-444444444444";
const CANDIDATE_ID = "55555555-5555-4555-8555-555555555555";
const TIMESTAMP = "2026-08-05T00:00:00Z";

const access = {
  apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
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

const revision = {
  id: REVISION_ID,
  factId: FACT_ID,
  revisionNumber: 1,
  revisionSequence: 1,
  content: "Prefers concise implementation plans",
  contentDigest: "a".repeat(64),
  category: "preference",
  confidence: 0.93,
  validFrom: TIMESTAMP,
  validTo: null,
  lastConfirmedAt: TIMESTAMP,
  changedBy: "user",
  sourceCandidateId: CANDIDATE_ID,
  supersedesRevisionId: null,
  changeReason: null,
  contentErasedAt: null,
  createdAt: TIMESTAMP,
};

const fact = {
  id: FACT_ID,
  factKind: "preference",
  status: "active",
  version: 1,
  disabledAt: null,
  supersededAt: null,
  deletedAt: null,
  createdAt: TIMESTAMP,
  updatedAt: TIMESTAMP,
  currentRevision: revision,
};

const candidate = {
  id: CANDIDATE_ID,
  candidateType: "preference",
  content: "Prefers concise implementation plans",
  confidence: 0.93,
  retentionClass: "durable",
  sensitivity: "normal",
  status: "pending",
  decisionReason: null,
  decidedAt: null,
  contentErasedAt: null,
  createdAt: TIMESTAMP,
  updatedAt: TIMESTAMP,
};

const evidence = {
  id: "66666666-6666-4666-8666-666666666666",
  factId: FACT_ID,
  revisionId: REVISION_ID,
  sourceCandidateId: CANDIDATE_ID,
  sourceItemId: null,
  threadId: "thread-1",
  runId: "run-1",
  runEventSequence: 1,
  evidenceExcerpt: "The user explicitly stated this preference.",
  trustClass: "direct",
  sourceErasedAt: null,
  createdAt: TIMESTAMP,
};

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("project Memory v2 client", () => {
  test("queues immediate consolidation without creating a chat request", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(
          {
            namespace: "default",
            disposition: "queued",
            jobId: "77777777-7777-4777-8777-777777777777",
            candidateCount: 2,
          },
          { status: 202 },
        ),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=memory-token" });
    rs.stubGlobal("fetch", fetcher);

    const result = await consolidateProjectMemoryV2(access);

    expect(result.disposition).toBe("queued");
    const [input, init] = fetcher.mock.calls[0]!;
    const url = new URL(requestURL(input), "http://local.test");
    expect(url.pathname).toBe(
      `/api/projects/${PROJECT_ID}/memory/v2/consolidate`,
    );
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
  });
  test("scopes list keys and sends database filters before pagination", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({ namespace: "default", items: [fact] }),
    );
    rs.stubGlobal("fetch", fetcher);

    const result = await listProjectMemoryV2Facts(
      access,
      {
        status: "all",
        limit: 26,
        offset: 25,
        query: "concise plan",
        category: "preference",
      },
      controller.signal,
    );

    expect(result.items).toHaveLength(1);
    const [url, init] = fetcher.mock.calls[0]!;
    const parsed = new URL(requestURL(url), "http://local.test");
    expect(parsed.pathname).toBe(`/api/projects/${PROJECT_ID}/memory/v2/facts`);
    expect(Object.fromEntries(parsed.searchParams)).toEqual({
      namespace: "default",
      status: "all",
      limit: "26",
      offset: "25",
      query: "concise plan",
      category: "preference",
    });
    expect(init?.signal).toBe(controller.signal);
    expect(
      projectMemoryV2FactsQueryKey(access.scope, {
        status: "all",
        limit: 26,
        offset: 25,
        query: "concise plan",
        category: "preference",
      }),
    ).toEqual(
      expect.arrayContaining([
        ACCOUNT_ID,
        PROJECT_ID,
        "facts",
        "all",
        "concise plan",
        "preference",
      ]),
    );
  });

  test("rejects unknown response fields instead of caching contract drift", async () => {
    rs.stubGlobal("fetch", async () =>
      Response.json({
        namespace: "default",
        items: [{ ...fact, unexpected: true }],
      }),
    );

    await expect(
      listProjectMemoryV2Facts(access, {
        status: "active",
        limit: 25,
        offset: 0,
      }),
    ).rejects.toThrow();
  });

  test("enforces the backend response length bounds", async () => {
    expect(
      memoryV2RevisionSchema.safeParse({
        ...revision,
        content: "x".repeat(16_000),
        changeReason: "x".repeat(64),
      }).success,
    ).toBe(true);
    expect(
      memoryV2RevisionSchema.safeParse({
        ...revision,
        content: "x".repeat(16_001),
      }).success,
    ).toBe(false);
    expect(
      memoryV2RevisionSchema.safeParse({
        ...revision,
        changeReason: "x".repeat(65),
      }).success,
    ).toBe(false);
    expect(
      memoryV2CandidateSchema.safeParse({
        ...candidate,
        content: "x".repeat(16_000),
        decisionReason: "x".repeat(64),
      }).success,
    ).toBe(true);
    expect(
      memoryV2CandidateSchema.safeParse({
        ...candidate,
        content: "x".repeat(16_001),
      }).success,
    ).toBe(false);
    expect(
      memoryV2CandidateSchema.safeParse({
        ...candidate,
        decisionReason: "x".repeat(65),
      }).success,
    ).toBe(false);
    expect(
      memoryV2EvidenceSchema.safeParse({
        ...evidence,
        evidenceExcerpt: "x".repeat(4_000),
        threadId: "x".repeat(64),
        runId: "x".repeat(64),
      }).success,
    ).toBe(true);
    expect(
      memoryV2EvidenceSchema.safeParse({
        ...evidence,
        evidenceExcerpt: "x".repeat(4_001),
      }).success,
    ).toBe(false);
    expect(
      memoryV2EvidenceSchema.safeParse({
        ...evidence,
        threadId: "x".repeat(65),
      }).success,
    ).toBe(false);
    expect(
      memoryV2EvidenceSchema.safeParse({
        ...evidence,
        runId: "x".repeat(65),
      }).success,
    ).toBe(false);

    rs.stubGlobal("fetch", async () =>
      Response.json({
        namespace: "x".repeat(128),
        items: [fact],
      }),
    );
    await expect(
      listProjectMemoryV2Facts(access, {
        status: "active",
        limit: 25,
        offset: 0,
      }),
    ).resolves.toBeDefined();

    rs.stubGlobal("fetch", async () =>
      Response.json({
        namespace: "x".repeat(129),
        items: [fact],
      }),
    );
    await expect(
      listProjectMemoryV2Facts(access, {
        status: "active",
        limit: 25,
        offset: 0,
      }),
    ).rejects.toThrow();
  });

  test("rejects oversized fact edits before sending a request", async () => {
    const fetcher = rs.fn(async () => Response.json(fact));
    rs.stubGlobal("fetch", fetcher);

    await expect(
      reviseProjectMemoryV2Fact(access, fact, {
        content: "x".repeat(16_001),
      }),
    ).rejects.toThrow();
    await expect(
      reviseProjectMemoryV2Fact(access, fact, {
        category: "x".repeat(33),
      }),
    ).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });

  test("uses exact candidate and fact CAS values for mutations", async () => {
    const fetcher = rs.fn(
      async (input: RequestInfo | URL, _init?: RequestInit) => {
        const path = new URL(requestURL(input), "http://local.test").pathname;
        if (path.endsWith("/accept")) return Response.json(fact);
        if (path.endsWith("/disable")) {
          return Response.json({
            ...fact,
            status: "disabled",
            version: 2,
            disabledAt: TIMESTAMP,
          });
        }
        if (path.endsWith("/hard-forget")) {
          return Response.json({
            factId: FACT_ID,
            version: 3,
            status: "deleted",
            erasedCandidates: 1,
            erasedRevisions: 2,
            erasedEvidence: 1,
            erasedSourceItems: 1,
          });
        }
        return Response.json({
          ...fact,
          version: 2,
          currentRevision: {
            ...revision,
            revisionNumber: 2,
            revisionSequence: 2,
            content: "Prefers executable implementation plans",
          },
        });
      },
    );
    rs.stubGlobal("document", { cookie: "csrf_token=memory-token" });
    rs.stubGlobal("fetch", fetcher);

    await acceptProjectMemoryV2Candidate(access, candidate);
    await reviseProjectMemoryV2Fact(access, fact, {
      content: "Prefers executable implementation plans",
      reason: "User correction",
    });
    await disableProjectMemoryV2Fact(access, fact);
    await hardForgetProjectMemoryV2Fact(access, fact);

    const bodies = fetcher.mock.calls.map((call) => jsonBody(call[1]));
    expect(bodies).toEqual([
      { expectedUpdatedAt: TIMESTAMP },
      {
        expectedVersion: 1,
        content: "Prefers executable implementation plans",
        reason: "User correction",
      },
      { expectedVersion: 1 },
      { expectedVersion: 1 },
    ]);
    for (const [, init] of fetcher.mock.calls) {
      expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
        "memory-token",
      );
    }
  });

  test("reads bounded status and downloads the NDJSON stream as a blob", async () => {
    const fetcher = rs.fn(async (input: RequestInfo | URL) => {
      const path = new URL(requestURL(input), "http://local.test").pathname;
      if (path.endsWith("/status")) {
        return Response.json({
          enabled: true,
          pipelineMode: "v2",
          searchEnabled: true,
          injectionEnabled: true,
          consolidationIntervalMinutes: 120,
          candidateRetentionDays: 30,
        });
      }
      return new Response('{"record_type":"manifest"}\n', {
        headers: { "Content-Type": "application/x-ndjson" },
      });
    });
    rs.stubGlobal("fetch", fetcher);

    const status = await getProjectMemoryV2Status(access);
    const blob = await exportProjectMemoryV2(access);

    expect(status.pipelineMode).toBe("v2");
    expect(await blob.text()).toContain('"record_type":"manifest"');
  });

  test("maps project capabilities without widening management access", () => {
    expect(projectMemoryV2Permissions(["private_work.read_own"])).toEqual({
      canRead: true,
      canExport: true,
      canManage: false,
      canHardForget: true,
    });
    expect(projectMemoryV2Permissions(["private_work.create"])).toEqual({
      canRead: false,
      canExport: false,
      canManage: true,
      canHardForget: false,
    });
  });
});
