import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  test,
  rs,
} from "@rstest/core";
import { cookies } from "next/headers";

rs.mock("next/headers", () => ({ cookies: rs.fn() }));
rs.mock("next/navigation", () => ({
  forbidden: rs.fn(() => {
    throw Object.assign(new Error("Forbidden"), { code: "NEXT_FORBIDDEN" });
  }),
  notFound: rs.fn(() => {
    throw Object.assign(new Error("Not found"), { code: "NEXT_NOT_FOUND" });
  }),
}));
rs.mock("@/core/auth/gateway-config", () => ({
  getGatewayConfig: () => ({ internalGatewayUrl: "http://gateway.test" }),
}));

import {
  lookupServerProjectBySlug,
  requireServerProjectCapability,
} from "@/core/projects/server-capability";

const originalFetch = globalThis.fetch;
const project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha-project",
  display_name: "Alpha Project",
  description: "",
  icon: "folder",
  role: "viewer",
  capabilities: ["project.read", "project.enter", "project.pin"],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 10 },
    storage_bytes: { used: 0, reserved: 0, limit: 1024 },
    concurrent_runs: { used: 0, reserved: 0, limit: 1 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 100 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-project",
  deletion_effective_at: null,
} as const;

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("server project capability boundary", () => {
  beforeEach(() => {
    rs.mocked(cookies).mockResolvedValue({
      get: () => ({ value: "session-token" }),
    } as never);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    rs.clearAllMocks();
  });

  test("returns an exact member project and forwards only the session cookie", async () => {
    const fetchMock = rs.fn(() =>
      Promise.resolve(response({ items: [project], next_cursor: null })),
    );
    globalThis.fetch = fetchMock as typeof fetch;

    await expect(lookupServerProjectBySlug("alpha-project")).resolves.toEqual({
      status: "ready",
      project,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("query=alpha-project"),
      expect.objectContaining({
        headers: { Cookie: "access_token=session-token" },
        cache: "no-store",
      }),
    );
  });

  test("returns 403 for a known member lacking capability and 404 for outsiders", async () => {
    globalThis.fetch = rs
      .fn()
      .mockResolvedValueOnce(response({ items: [project], next_cursor: null }))
      .mockResolvedValueOnce(
        response({ items: [], next_cursor: null }),
      ) as typeof fetch;

    await expect(
      requireServerProjectCapability("alpha-project", "project.audit.read"),
    ).rejects.toMatchObject({ code: "NEXT_FORBIDDEN" });
    await expect(
      requireServerProjectCapability("missing-project", "project.audit.read"),
    ).rejects.toMatchObject({ code: "NEXT_NOT_FOUND" });
  });

  test("does not turn gateway failure into an authorization decision", async () => {
    globalThis.fetch = rs.fn(() =>
      Promise.reject(new Error("offline")),
    ) as typeof fetch;

    await expect(
      requireServerProjectCapability("alpha-project", "project.audit.read"),
    ).resolves.toBeUndefined();
    await expect(lookupServerProjectBySlug("../invalid")).resolves.toEqual({
      status: "not_found",
    });
  });

  test("all governance-only project routes enforce the server boundary", () => {
    const expectedRoutes = [
      ["members/page.tsx", "project.members.manage"],
      ["credentials/page.tsx", "mcp.credentials.approve"],
      ["settings/audit/page.tsx", "project.audit.read"],
      ["settings/usage/page.tsx", "project.usage.read"],
    ] as const;

    for (const [route, capability] of expectedRoutes) {
      const source = readFileSync(
        resolve(process.cwd(), "src/app/projects/[project_slug]", route),
        "utf8",
      );
      expect(source).toContain("requireServerProjectCapability");
      expect(source).toContain(`\"${capability}\"`);
    }
  });
});
