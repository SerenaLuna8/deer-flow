import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  fetchProjectPrivateWorkReadiness,
  projectPrivateWorkEntryEnabled,
} from "@/core/private-work/readiness";

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("project private-work readiness", () => {
  test("loads readiness from the strict project private-work base", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          status: "ready",
          code: "PRIVATE_WORK_READY",
          request_id: "req-ready",
        }),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      fetchProjectPrivateWorkReadiness({
        apiBaseURL:
          "http://localhost:2026/api/projects/11111111-1111-4111-8111-111111111111/private-work",
      }),
    ).resolves.toMatchObject({ status: "ready" });
    const requested = fetcher.mock.calls[0]![0];
    const requestedURL =
      typeof requested === "string"
        ? requested
        : requested instanceof URL
          ? requested.href
          : requested.url;
    expect(requestedURL).toMatch(
      /\/api\/projects\/11111111-1111-4111-8111-111111111111\/private-work\/readiness$/u,
    );
  });

  test("opens the CTA only after feature, capability, and readiness gates", () => {
    expect(projectPrivateWorkEntryEnabled(true, true, "ready")).toBe(true);
    expect(projectPrivateWorkEntryEnabled(false, true, "ready")).toBe(false);
    expect(projectPrivateWorkEntryEnabled(true, false, "ready")).toBe(false);
    expect(
      projectPrivateWorkEntryEnabled(true, true, "migration_required"),
    ).toBe(false);
    expect(projectPrivateWorkEntryEnabled(true, true, undefined)).toBe(false);
  });
});
