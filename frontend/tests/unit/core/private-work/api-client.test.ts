import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  disposeProjectAPIClient,
  getProjectAPIClient,
  projectPrivateWorkBaseURL,
} from "@/core/private-work/api-client";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const P1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const P2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function apiUrlOf(client: object): string {
  return String(Reflect.get(Reflect.get(client, "threads"), "apiUrl"));
}

afterEach(() => {
  disposeProjectAPIClient({ accountId: A, projectId: P1 });
  disposeProjectAPIClient({ accountId: A, projectId: P2 });
  disposeProjectAPIClient({ accountId: B, projectId: P1 });
  rs.unstubAllGlobals();
});

describe("project private-work API client", () => {
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
  });

  test("builds only a strict UUID project base URL", () => {
    expect(
      projectPrivateWorkBaseURL(P1).endsWith(
        `/api/projects/${P1}/private-work`,
      ),
    ).toBe(true);
    expect(() => projectPrivateWorkBaseURL("../admin")).toThrow();
  });

  test("keeps the existing CSRF request wrapper", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => {
        return new Response(JSON.stringify({ thread_id: P1 }), {
          status: 200,
        });
      },
    );
    rs.stubGlobal("document", { cookie: "csrf_token=project-token" });
    rs.stubGlobal("fetch", fetcher);

    const client = getProjectAPIClient({ accountId: A, projectId: P1 });
    await client.threads.create();

    const init = fetcher.mock.calls[0]![1]!;
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("project-token");
  });
});
