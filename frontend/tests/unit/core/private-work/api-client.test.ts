import { afterEach, describe, expect, test, rs } from "@rstest/core";

import { createProjectPrivateClient } from "@/core/api/api-client";
import {
  createProjectThread,
  disposeProjectAPIClient,
  getProjectAPIClient,
  projectPrivateWorkBaseURL,
} from "@/core/private-work/api-client";
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
});
