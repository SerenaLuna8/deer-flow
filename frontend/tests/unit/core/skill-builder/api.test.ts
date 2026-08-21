import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  SkillBuilderApiError,
  listSkillBuilderActivities,
  mergeSkillBuilderActivities,
  stopSkillBuilderTurn,
  setSkillBuilderExecutionPreference,
  skillBuilderActivityTerminal,
  submitSkillBuilderTurn,
} from "@/core/skill-builder";

const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const RUN_ID = "44444444-4444-4444-8444-444444444444";
const NOW = "2026-08-13T08:00:00+08:00";

function legacyResponse() {
  return {
    data: {
      id: SESSION_ID,
      project_id: PROJECT_ID,
      owner_user_id: "owner-1",
      thread_id: THREAD_ID,
      slug: "catalog-auditor",
      display_name: "Catalog auditor",
      status: "interviewing",
      revision: 2,
      messages: [],
      active_clarification: null,
      progress: [],
      files: [],
      draft_checksum: null,
      validation: null,
      error_code: null,
      error_message: null,
      created_skill_id: null,
      session_kind: "create",
      target_skill_id: null,
      base_version_id: null,
      base_version_number: null,
      base_payload_checksum: null,
      target_skill_deleted: false,
      base_files: [],
      created_at: NOW,
      updated_at: NOW,
    },
    request_id: "request-1",
  };
}

function jsonBody(init: RequestInit | undefined) {
  if (typeof init?.body !== "string") {
    throw new Error("Expected a JSON request body");
  }
  return JSON.parse(init.body) as unknown;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("submitSkillBuilderTurn", () => {
  test("continues to parse the legacy synchronous session response", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () => Response.json(legacyResponse()));

    await expect(
      submitSkillBuilderTurn(PROJECT_ID, SESSION_ID, {
        input: {
          kind: "message",
          message: "Create a catalog auditor",
        },
        expected_revision: 1,
        idempotency_key: "turn-1",
      }),
    ).resolves.toEqual(legacyResponse());
  });

  test("parses Run admission without changing the request contract", async () => {
    const admission = {
      runId: RUN_ID,
      status: "running" as const,
      streamUrl: `/api/projects/${PROJECT_ID}/skill-builder/runs/${RUN_ID}/stream`,
    };
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(admission),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await expect(
      submitSkillBuilderTurn(PROJECT_ID, SESSION_ID, {
        input: {
          kind: "message",
          message: "Use the available catalog tool",
        },
        expected_revision: 3,
        idempotency_key: "turn-2",
      }),
    ).resolves.toEqual(admission);
    expect(jsonBody(fetcher.mock.calls[0]?.[1])).toEqual({
      input: {
        kind: "message",
        message: "Use the available catalog tool",
      },
      expected_revision: 3,
      idempotency_key: "turn-2",
    });
  });

  test("fails closed when a successful response adds unknown fields", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({
        runId: RUN_ID,
        status: "pending",
        streamUrl: "/api/stream",
        owner_user_id: "unexpected-authority",
      }),
    );

    const request = submitSkillBuilderTurn(PROJECT_ID, SESSION_ID, {
      input: {
        kind: "message",
        message: "Create a Skill",
      },
      expected_revision: 1,
      idempotency_key: "turn-3",
    });

    await expect(request).rejects.toBeInstanceOf(SkillBuilderApiError);
    await expect(request).rejects.toMatchObject({
      status: 200,
      code: "SKILL_BUILDER_RESPONSE_INVALID",
    });
  });
});

describe("setSkillBuilderExecutionPreference", () => {
  test("persists one complete capability-normalized session preference", async () => {
    const response = legacyResponse();
    response.data.revision = 3;
    Object.assign(response.data, {
      execution_preference: {
        model_name: "00000000-0000-4000-8000-000000000205",
        mode: "pro",
        thinking_enabled: true,
        reasoning_effort: "medium",
      },
    });
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(response),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await expect(
      setSkillBuilderExecutionPreference(PROJECT_ID, SESSION_ID, {
        model_name: "00000000-0000-4000-8000-000000000205",
        mode: "pro",
        thinking_enabled: true,
        reasoning_effort: "medium",
      }),
    ).resolves.toEqual(response);
    expect(jsonBody(fetcher.mock.calls[0]?.[1])).toEqual({
      model_name: "00000000-0000-4000-8000-000000000205",
      mode: "pro",
      thinking_enabled: true,
      reasoning_effort: "medium",
    });
  });
});

describe("Skill Builder Activity API", () => {
  test("treats only legacy standalone validation results as terminal", () => {
    const validation = {
      seq: "2",
      operation_id: "55555555-5555-4555-8555-555555555555",
      run_id: null,
      kind: "validation_passed" as const,
      attempt: null,
      payload: {},
      created_at: NOW,
    };

    expect(skillBuilderActivityTerminal([validation])?.status).toBe(
      "completed",
    );
    expect(
      skillBuilderActivityTerminal([{ ...validation, run_id: RUN_ID }]),
    ).toBeNull();
  });

  test("drops duplicate and non-increasing Activity frames", () => {
    const activity = {
      seq: "2",
      operation_id: "55555555-5555-4555-8555-555555555555",
      run_id: RUN_ID,
      kind: "reasoning" as const,
      attempt: 1,
      payload: { text: "已回放" },
      created_at: NOW,
    };

    expect(
      mergeSkillBuilderActivities(
        [activity],
        [
          { ...activity, payload: { text: "重复帧" } },
          { ...activity, seq: "1", payload: { text: "倒退帧" } },
          { ...activity, seq: "3", payload: { text: "实时增量" } },
        ],
      ).map((item) => [item.seq, item.payload]),
    ).toEqual([
      ["2", { text: "已回放" }],
      ["3", { text: "实时增量" }],
    ]);
  });

  test("parses only the isolated public-safe Activity contract", async () => {
    const response = {
      data: [
        {
          seq: "9007199254740993",
          operation_id: "55555555-5555-4555-8555-555555555555",
          run_id: RUN_ID,
          kind: "reasoning",
          attempt: 1,
          payload: { text: "真实思考" },
          created_at: NOW,
        },
      ],
      request_id: "request-activity",
    };
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () => Response.json(response));

    await expect(
      listSkillBuilderActivities(PROJECT_ID, SESSION_ID),
    ).resolves.toEqual(response);
  });

  test("rejects tool arguments and results from a successful Activity response", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({
        data: [
          {
            seq: "1",
            operation_id: "55555555-5555-4555-8555-555555555555",
            run_id: RUN_ID,
            kind: "tool_started",
            attempt: 1,
            payload: {
              tool_call_id: "call-1",
              tool_name: "read_candidate_file",
              args: { secret: "must-not-enter-cache" },
            },
            created_at: NOW,
          },
        ],
        request_id: "request-activity",
      }),
    );

    await expect(
      listSkillBuilderActivities(PROJECT_ID, SESSION_ID),
    ).rejects.toMatchObject({ code: "SKILL_BUILDER_RESPONSE_INVALID" });
  });

  test("stops only the current turn without a request body", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(legacyResponse()),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=builder-token" });
    rs.stubGlobal("fetch", fetcher);

    await stopSkillBuilderTurn(PROJECT_ID, SESSION_ID);

    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({ method: "POST" });
    expect(fetcher.mock.calls[0]?.[1]?.body).toBeUndefined();
  });
});
