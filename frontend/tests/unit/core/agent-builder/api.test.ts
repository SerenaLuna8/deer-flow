import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  AgentBuilderApiError,
  cancelAgentBuilderSession,
  createAgentBuilderSession,
  finalizeAgentBuilderSession,
  getAgentBuilderSession,
  listAllAgentBuilderActivities,
  listAllAgentBuilderSessions,
  listAgentBuilderSessions,
  mergeAgentBuilderActivities,
  setAgentBuilderGenerationPreference,
  submitAgentBuilderTurn,
} from "@/core/agent-builder";

const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const NOW = "2026-08-18T08:00:00+08:00";

function sessionResponse() {
  return {
    data: {
      id: SESSION_ID,
      project_id: PROJECT_ID,
      owner_user_id: "owner-1",
      thread_id: THREAD_ID,
      slug: "catalog-auditor",
      display_name: "Catalog auditor",
      status: "proposal_ready",
      revision: 2,
      blueprint: null,
      blueprint_checksum: null,
      assumptions: ["仅审查当前项目"],
      conflicts: [
        {
          code: "AMBIGUOUS_SCOPE",
          fields: ["agents_instructions"],
          message: "审查范围需要确认",
          severity: "warning",
        },
      ],
      messages: [],
      active_clarification: null,
      active_clarifications: [],
      progress: [],
      error_code: null,
      error_message: null,
      created_agent_id: null,
      generation_preference: {
        model_ref: "00000000-0000-4000-8000-000000000204",
        mode: "pro",
      },
      created_at: NOW,
      updated_at: NOW,
    },
    request_id: "request-1",
  };
}

function commitResponse() {
  return {
    data: {
      session: sessionResponse().data,
      agent: {
        id: "77777777-7777-4777-8777-777777777777",
        scope: "project",
        project_id: PROJECT_ID,
        slug: "renamed-agent",
        display_name: "renamed-agent",
        status: "suspended",
        current_version_id: null,
        revision: 1,
        created_by_user_id: "owner-1",
        created_at: NOW,
        updated_at: NOW,
      },
    },
    request_id: "request-commit",
  };
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("Agent Builder API", () => {
  test("deduplicates and orders Activity cursors without JavaScript number coercion", () => {
    const shared = {
      operation_id: "44444444-4444-4444-8444-444444444444",
      attempt: null,
      payload: {},
      created_at: NOW,
    } as const;

    expect(
      mergeAgentBuilderActivities(
        [{ ...shared, seq: "9007199254740993", kind: "turn_accepted" }],
        [
          { ...shared, seq: "9007199254740994", kind: "attempt_started" },
          { ...shared, seq: "9007199254740993", kind: "turn_accepted" },
        ],
      ).map((activity) => activity.seq),
    ).toEqual(["9007199254740993", "9007199254740994"]);

    const terminal = {
      ...shared,
      seq: "9007199254740995",
      kind: "turn_terminal" as const,
      payload: { status: "completed" as const },
    };
    expect(
      mergeAgentBuilderActivities(
        [
          { ...shared, seq: "9007199254740993", kind: "turn_accepted" },
          terminal,
        ],
        [{ ...shared, seq: "9007199254740994", kind: "attempt_started" }],
      ).at(-1),
    ).toEqual(terminal);
  });

  test("paginates durable Activity replay until the terminal event is included", async () => {
    const operationId = "44444444-4444-4444-8444-444444444444";
    const firstPage = Array.from({ length: 2_000 }, (_, index) => ({
      seq: String(index + 1),
      operation_id: operationId,
      kind: index === 0 ? ("turn_accepted" as const) : ("reasoning" as const),
      attempt: index === 0 ? null : (1 as const),
      payload: index === 0 ? {} : { text: `delta-${index}` },
      created_at: NOW,
    }));
    const terminal = {
      seq: "2001",
      operation_id: operationId,
      kind: "turn_terminal" as const,
      attempt: null,
      payload: { status: "completed" as const, duration_ms: 80_000 },
      created_at: NOW,
    };
    const requestedCursors: string[] = [];
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      const rawUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const url = new URL(rawUrl, "http://frontend.test");
      const cursor = url.searchParams.get("after_seq") ?? "0";
      requestedCursors.push(cursor);
      return Response.json({
        data: cursor === "0" ? firstPage : [terminal],
        request_id: `activity-page-${cursor}`,
      });
    });

    const activities = await listAllAgentBuilderActivities(
      PROJECT_ID,
      SESSION_ID,
    );

    expect(requestedCursors).toEqual(["0", "2000"]);
    expect(activities).toHaveLength(2_001);
    expect(activities.at(-1)).toEqual(terminal);
  });

  test("requests contract version 3 on every Agent Builder endpoint", async () => {
    const fetcher = rs.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const rawUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        const url = new URL(rawUrl, "http://frontend.test");
        if (url.pathname.endsWith("/commit")) {
          return Response.json(commitResponse());
        }
        if (
          url.pathname.endsWith("/sessions") &&
          (init?.method === undefined || init.method === "GET")
        ) {
          return Response.json({
            data: [],
            next_cursor: null,
            request_id: "request-list",
          });
        }
        return Response.json(sessionResponse());
      },
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await createAgentBuilderSession(PROJECT_ID, {
      slug: "contract-agent",
      display_name: "contract-agent",
      idempotency_key: "create-key",
    });
    await getAgentBuilderSession(PROJECT_ID, SESSION_ID);
    await listAgentBuilderSessions(PROJECT_ID, {
      limit: 20,
      cursor: "opaque-cursor",
    });
    await submitAgentBuilderTurn(PROJECT_ID, SESSION_ID, {
      input: { kind: "message", message: "Design an Agent" },
      generation_model_ref: "00000000-0000-4000-8000-000000000204",
      generation_mode: "pro",
      thinking_enabled: true,
      reasoning_effort: "medium",
      expected_revision: 2,
      idempotency_key: "turn-key",
    });
    await setAgentBuilderGenerationPreference(PROJECT_ID, SESSION_ID, {
      generation_model_ref: "00000000-0000-4000-8000-000000000204",
      generation_mode: "ultra",
      thinking_enabled: true,
      reasoning_effort: "high",
    });
    await finalizeAgentBuilderSession(PROJECT_ID, SESSION_ID, {
      expected_revision: 2,
      expected_blueprint_checksum: "checksum",
      idempotency_key: "commit-key",
    });
    await cancelAgentBuilderSession(PROJECT_ID, SESSION_ID, {
      expected_revision: 2,
      idempotency_key: "cancel-key",
    });

    expect(fetcher).toHaveBeenCalledTimes(7);
    for (const [input] of fetcher.mock.calls) {
      const rawUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      expect(
        new URL(rawUrl, "http://frontend.test").searchParams.get(
          "contract_version",
        ),
      ).toBe("3");
    }
  });

  test("sends the complete Builder generation profile", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(sessionResponse()),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await submitAgentBuilderTurn(PROJECT_ID, SESSION_ID, {
      input: { kind: "message", message: "Design an Agent" },
      generation_model_ref: "00000000-0000-4000-8000-000000000204",
      generation_mode: "ultra",
      thinking_enabled: true,
      reasoning_effort: "high",
      expected_revision: 2,
      idempotency_key: "turn-profile-key",
    });

    const body = fetcher.mock.calls[0]?.[1]?.body;
    expect(typeof body).toBe("string");
    if (typeof body !== "string") throw new Error("expected JSON body");
    expect(JSON.parse(body)).toMatchObject({
      generation_model_ref: "00000000-0000-4000-8000-000000000204",
      generation_mode: "ultra",
      thinking_enabled: true,
      reasoning_effort: "high",
    });
  });

  test("sends the selected normalized slug with the commit command", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(commitResponse()),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await finalizeAgentBuilderSession(PROJECT_ID, SESSION_ID, {
      slug: "renamed-agent",
      expected_revision: 2,
      expected_blueprint_checksum: "checksum",
      idempotency_key: "commit-key",
    });

    const init = fetcher.mock.calls[0]?.[1];
    const body = init?.body;
    expect(typeof body).toBe("string");
    if (typeof body !== "string") {
      throw new Error("Agent Builder commit body must be JSON text");
    }
    expect(JSON.parse(body)).toEqual({
      slug: "renamed-agent",
      expected_revision: 2,
      expected_blueprint_checksum: "checksum",
      idempotency_key: "commit-key",
    });
  });

  test("keeps the default commit body compatible with a backend that does not know slug", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(commitResponse()),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await finalizeAgentBuilderSession(PROJECT_ID, SESSION_ID, {
      expected_revision: 2,
      expected_blueprint_checksum: "checksum",
      idempotency_key: "commit-key",
    });

    const body = fetcher.mock.calls[0]?.[1]?.body;
    expect(typeof body).toBe("string");
    if (typeof body !== "string") {
      throw new Error("Agent Builder commit body must be JSON text");
    }
    expect(JSON.parse(body)).toEqual({
      expected_revision: 2,
      expected_blueprint_checksum: "checksum",
      idempotency_key: "commit-key",
    });
  });

  test("parses assumptions and structured conflicts on a session", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () => Response.json(sessionResponse()));

    await expect(
      getAgentBuilderSession(PROJECT_ID, SESSION_ID),
    ).resolves.toEqual(sessionResponse());
  });

  test("accepts the previous strict session shape during a rolling deployment", async () => {
    const current = sessionResponse();
    const legacyData: Record<string, unknown> = { ...current.data };
    delete legacyData.assumptions;
    delete legacyData.conflicts;
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({ ...current, data: legacyData }),
    );

    await expect(
      getAgentBuilderSession(PROJECT_ID, SESSION_ID),
    ).resolves.toMatchObject({
      data: { assumptions: [], conflicts: [] },
    });
  });

  test("keeps rolling-deployment defaults while rejecting unknown session fields", async () => {
    const current = sessionResponse();
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({
        ...current,
        data: { ...current.data, credential_secret: "must-not-reach-ui" },
      }),
    );

    await expect(
      getAgentBuilderSession(PROJECT_ID, SESSION_ID),
    ).rejects.toMatchObject({ code: "AGENT_BUILDER_RESPONSE_INVALID" });
  });

  test("supports the authoritative cursor pagination contract", async () => {
    const page = {
      data: [],
      next_cursor: "next-page",
      request_id: "request-list",
    };
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(page),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await expect(
      listAgentBuilderSessions(PROJECT_ID, {
        limit: 50,
        cursor: "opaque+cursor",
      }),
    ).resolves.toEqual(page);
    const requested = fetcher.mock.calls[0]?.[0];
    const requestedUrl =
      typeof requested === "string"
        ? requested
        : requested instanceof URL
          ? requested.href
          : requested?.url;
    expect(requestedUrl).toContain("limit=50&cursor=opaque%2Bcursor");
  });

  test("treats a missing next cursor from the previous response shape as the last page", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({ data: [], request_id: "legacy-list" }),
    );

    await expect(listAgentBuilderSessions(PROJECT_ID)).resolves.toMatchObject({
      data: [],
      next_cursor: null,
    });
  });

  test("follows every opaque cursor when loading the resume list", async () => {
    const firstId = "44444444-4444-4444-8444-444444444444";
    const secondId = "55555555-5555-4555-8555-555555555555";
    const thirdId = "66666666-6666-4666-8666-666666666666";
    const cursor = "opaque+/=cursor";
    const fetcher = rs.fn(async (input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const continuing = url.includes("cursor=");
      return Response.json({
        data: continuing
          ? [
              {
                id: secondId,
                slug: "second",
                display_name: "Second",
                status: "interviewing",
                revision: 1,
                updated_at: NOW,
              },
              {
                id: thirdId,
                slug: "third",
                display_name: "Third",
                status: "interviewing",
                revision: 1,
                updated_at: NOW,
              },
            ]
          : [
              {
                id: firstId,
                slug: "first",
                display_name: "First",
                status: "interviewing",
                revision: 1,
                updated_at: "2026-08-17T08:00:00+08:00",
              },
            ],
        next_cursor: continuing ? null : cursor,
        request_id: continuing ? "page-2" : "page-1",
      });
    });
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await expect(listAllAgentBuilderSessions(PROJECT_ID)).resolves.toEqual([
      expect.objectContaining({ id: thirdId }),
      expect.objectContaining({ id: secondId }),
      expect.objectContaining({ id: firstId }),
    ]);
    expect(fetcher).toHaveBeenCalledTimes(2);
    const secondRequest = fetcher.mock.calls[1]?.[0];
    const secondUrl =
      typeof secondRequest === "string"
        ? secondRequest
        : secondRequest instanceof URL
          ? secondRequest.href
          : secondRequest?.url;
    expect(secondUrl).toContain("cursor=opaque%2B%2F%3Dcursor");
  });

  test("rejects a duplicate session id across cursor pages", async () => {
    let page = 0;
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () => {
      page += 1;
      return Response.json({
        data: [
          {
            id: SESSION_ID,
            slug: "duplicate",
            display_name: "Duplicate",
            status: "interviewing",
            revision: page,
            updated_at: NOW,
          },
        ],
        next_cursor: page === 1 ? "page-2" : null,
        request_id: `page-${page}`,
      });
    });

    await expect(listAllAgentBuilderSessions(PROJECT_ID)).rejects.toMatchObject(
      {
        code: "AGENT_BUILDER_RESPONSE_INVALID",
      },
    );
  });

  test("continues when a concurrent terminal-state filter leaves an empty page with a new cursor", async () => {
    const firstId = "44444444-4444-4444-8444-444444444444";
    const lastId = "55555555-5555-4555-8555-555555555555";
    let page = 0;
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    const fetcher = rs.fn(async () => {
      page += 1;
      return Response.json(
        page === 1
          ? {
              data: [
                {
                  id: firstId,
                  slug: "first",
                  display_name: "First",
                  status: "interviewing",
                  revision: 1,
                  updated_at: "2026-08-17T08:00:00+08:00",
                },
              ],
              next_cursor: "after-first",
              request_id: "page-1",
            }
          : page === 2
            ? {
                data: [],
                next_cursor: "after-filtered-terminal-row",
                request_id: "page-2",
              }
            : {
                data: [
                  {
                    id: lastId,
                    slug: "last",
                    display_name: "Last",
                    status: "interviewing",
                    revision: 1,
                    updated_at: NOW,
                  },
                ],
                next_cursor: null,
                request_id: "page-3",
              },
      );
    });
    rs.stubGlobal("fetch", fetcher);

    await expect(listAllAgentBuilderSessions(PROJECT_ID)).resolves.toEqual([
      expect.objectContaining({ id: lastId }),
      expect.objectContaining({ id: firstId }),
    ]);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  test("rejects an empty page whose next cursor does not advance", async () => {
    let page = 0;
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    const fetcher = rs.fn(async () => {
      page += 1;
      return Response.json({
        data: [],
        next_cursor: "unchanged-cursor",
        request_id: `page-${page}`,
      });
    });
    rs.stubGlobal("fetch", fetcher);

    await expect(listAllAgentBuilderSessions(PROJECT_ID)).rejects.toMatchObject(
      {
        code: "AGENT_BUILDER_RESPONSE_INVALID",
      },
    );
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  test("rejects a page larger than the requested safety limit", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({
        data: Array.from({ length: 101 }, (_, index) => ({
          id: `00000000-0000-4000-8000-${(index + 1).toString().padStart(12, "0")}`,
          slug: `session-${index + 1}`,
          display_name: `Session ${index + 1}`,
          status: "interviewing",
          revision: 1,
          updated_at: NOW,
        })),
        next_cursor: null,
        request_id: "oversized-page",
      }),
    );

    await expect(listAllAgentBuilderSessions(PROJECT_ID)).rejects.toMatchObject(
      {
        code: "AGENT_BUILDER_RESPONSE_INVALID",
      },
    );
  });

  test("bounds a non-terminating unique-cursor pagination stream", async () => {
    let page = 0;
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    const fetcher = rs.fn(async () => {
      page += 1;
      return Response.json({
        data: [
          {
            id: `00000000-0000-4000-8000-${page.toString().padStart(12, "0")}`,
            slug: `session-${page}`,
            display_name: `Session ${page}`,
            status: "interviewing",
            revision: 1,
            updated_at: NOW,
          },
        ],
        next_cursor: `page-${page + 1}`,
        request_id: `page-${page}`,
      });
    });
    rs.stubGlobal("fetch", fetcher);

    await expect(listAllAgentBuilderSessions(PROJECT_ID)).rejects.toMatchObject(
      {
        code: "AGENT_BUILDER_RESPONSE_INVALID",
      },
    );
    expect(fetcher).toHaveBeenCalledTimes(100);
  });

  test("preserves domain 409 codes instead of misclassifying them as revision conflicts", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json(
        {
          detail: {
            code: "AGENT_DESIGN_CONFLICT_UNRESOLVED",
            message: "Agent design has unresolved error conflicts",
          },
        },
        { status: 409 },
      ),
    );

    const request = listAgentBuilderSessions(PROJECT_ID);
    await expect(request).rejects.toBeInstanceOf(AgentBuilderApiError);
    await expect(request).rejects.toMatchObject({
      status: 409,
      code: "AGENT_DESIGN_CONFLICT_UNRESOLVED",
    });
  });

  test("preserves the unfinished-session limit as an actionable 429 domain error", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json(
        {
          detail: {
            code: "AGENT_DESIGN_SESSION_LIMIT_EXCEEDED",
            message: "Too many unfinished Agent design sessions",
          },
        },
        { status: 429 },
      ),
    );

    await expect(
      createAgentBuilderSession(PROJECT_ID, {
        slug: "new-reviewer",
        display_name: "new-reviewer",
        idempotency_key: "create-key",
      }),
    ).rejects.toMatchObject({
      status: 429,
      code: "AGENT_DESIGN_SESSION_LIMIT_EXCEEDED",
    });
  });
});
