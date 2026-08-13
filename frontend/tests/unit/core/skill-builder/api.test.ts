import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  SkillBuilderApiError,
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
