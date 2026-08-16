import { afterEach, describe, expect, test, rs } from "@rstest/core";

import { createProjectPrivateClient } from "@/core/api/api-client";
import {
  createProjectThread,
  deleteProjectThread,
  disposeProjectAPIClient,
  getProjectAPIClient,
  projectPrivateWorkBaseURL,
} from "@/core/private-work/api-client";
import type { ProjectResponseError } from "@/core/private-work/api-client";
import { projectClientScopeSchema } from "@/core/private-work/types";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const P1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const P2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function apiUrlOf(client: object): string {
  return String(Reflect.get(Reflect.get(client, "threads"), "apiUrl"));
}

function maxConcurrencyOf(client: object): number {
  return Number(
    Reflect.get(
      Reflect.get(Reflect.get(client, "runs"), "asyncCaller"),
      "maxConcurrency",
    ),
  );
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

afterEach(() => {
  disposeProjectAPIClient({ accountId: A, projectId: P1 });
  disposeProjectAPIClient({ accountId: A, projectId: P2 });
  disposeProjectAPIClient({ accountId: B, projectId: P1 });
  rs.unstubAllGlobals();
});

describe("project private-work API client", () => {
  test("does not retry browser-aborted SDK requests", async () => {
    const browserAbort = Object.assign(
      new Error("This operation was aborted"),
      { name: "AbortError" },
    );
    const fetcher = rs.fn(async () => {
      throw browserAbort;
    });
    rs.stubGlobal("fetch", fetcher);

    const client = createProjectPrivateClient({
      apiUrl: "http://localhost/project-private-work",
    });
    await expect(client.runs.list(P1)).rejects.toMatchObject({
      name: "AbortError",
      message: "AbortError",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  test("does not share a LangGraph client across accounts or projects", () => {
    const a1 = getProjectAPIClient({ accountId: A, projectId: P1 });
    const a2 = getProjectAPIClient({ accountId: A, projectId: P2 });
    const b1 = getProjectAPIClient({ accountId: B, projectId: P1 });

    expect(a1).not.toBe(a2);
    expect(a1).not.toBe(b1);
    expect(getProjectAPIClient({ accountId: A, projectId: P1 })).toBe(a1);
    expect(apiUrlOf(a1).endsWith(`/api/projects/${P1}/private-work`)).toBe(
      true,
    );
    expect(maxConcurrencyOf(a1)).toBe(6);
  });

  test("builds only a strict UUID project base URL", () => {
    expect(
      projectPrivateWorkBaseURL(P1).endsWith(
        `/api/projects/${P1}/private-work`,
      ),
    ).toBe(true);
    expect(() => projectPrivateWorkBaseURL("../admin")).toThrow();
  });

  test("accepts only UUID accounts plus the canonical local auth-disabled account", () => {
    expect(
      projectClientScopeSchema.parse({ accountId: "default", projectId: P1 }),
    ).toEqual({ accountId: "default", projectId: P1 });
    expect(() =>
      projectClientScopeSchema.parse({ accountId: "someone", projectId: P1 }),
    ).toThrow();
  });

  test("keeps the existing CSRF request wrapper", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => {
        return new Response(
          JSON.stringify({
            thread_id: P1,
            agent_asset_id: P2,
            agent_scope: "project",
            display_name: "CSRF thread",
            status: "idle",
            metadata: {},
            version: 1,
            created_at: "2026-07-21T06:00:00Z",
            updated_at: "2026-07-21T06:00:00Z",
          }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" },
          },
        );
      },
    );
    rs.stubGlobal("document", { cookie: "csrf_token=project-token" });
    rs.stubGlobal("fetch", fetcher);

    await createProjectThread(
      { accountId: A, projectId: P1 },
      { threadId: P1, agentAssetId: P2 },
    );

    const init = fetcher.mock.calls[0]![1]!;
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("project-token");
  });

  test("refreshes the CAS version for delete and preserves structured gateway errors", async () => {
    const fetcher = rs.fn(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.endsWith(`/threads/${P1}`)) {
        return new Response(
          JSON.stringify({
            thread_id: P1,
            agent_asset_id: P2,
            agent_scope: "project",
            display_name: "Fresh thread",
            status: "idle",
            metadata: {},
            version: 7,
            created_at: "2026-07-21T06:00:00Z",
            updated_at: "2026-07-21T06:00:00Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes(`/threads/${P1}?expected_version=7`)) {
        return new Response(
          JSON.stringify({
            detail: {
              code: "PRIVATE_WORK_CONFLICT",
              message: "Private work conflict.",
              request_id: "request-delete-1",
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    rs.stubGlobal("fetch", fetcher);

    await expect(
      deleteProjectThread(
        { accountId: A, projectId: P1 },
        { threadId: P1, expectedVersion: 7 },
      ),
    ).rejects.toMatchObject({
      name: "ProjectResponseError",
      status: 409,
      code: "PRIVATE_WORK_CONFLICT",
      requestId: "request-delete-1",
      serverMessage: "Private work conflict.",
    } satisfies Partial<ProjectResponseError>);

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(requestUrl(fetcher.mock.calls[0]![0])).toContain(
      `/threads/${P1}?expected_version=7`,
    );
    expect(requestUrl(fetcher.mock.calls[1]![0])).toMatch(
      new RegExp(`/threads/${P1}$`),
    );
  });

  test("treats a scoped delete 404 as idempotent but preserves 403", async () => {
    const fetcher = rs
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: {
              code: "PRIVATE_WORK_NOT_FOUND",
              message: "Private work not found.",
              request_id: "request-delete-gone",
            },
          }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: {
              code: "PRIVATE_WORK_FORBIDDEN",
              message: "Private work forbidden.",
              request_id: "request-delete-forbidden",
            },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
      );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      deleteProjectThread(
        { accountId: A, projectId: P1 },
        { threadId: P1, expectedVersion: 1 },
      ),
    ).resolves.toBeUndefined();
    await expect(
      deleteProjectThread(
        { accountId: A, projectId: P1 },
        { threadId: P1, expectedVersion: 1 },
      ),
    ).rejects.toMatchObject({
      status: 403,
      code: "PRIVATE_WORK_FORBIDDEN",
      requestId: "request-delete-forbidden",
    } satisfies Partial<ProjectResponseError>);
  });
});
