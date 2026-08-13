import { describe, expect, test } from "@rstest/core";

import {
  isSkillBuilderRunAdmission,
  skillBuilderPollingInterval,
  skillBuilderRunPresentation,
  skillBuilderRunStreamProjectionSchema,
  skillBuilderSessionResponseSchema,
  skillBuilderTurnResponseSchema,
  type SkillBuilderSession,
} from "@/core/skill-builder";

const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const RUN_ID = "44444444-4444-4444-8444-444444444444";
const NOW = "2026-08-13T08:00:00+08:00";

function session(
  overrides: Partial<SkillBuilderSession> = {},
): SkillBuilderSession {
  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: "owner-1",
    thread_id: THREAD_ID,
    slug: "catalog-auditor",
    display_name: "Catalog auditor",
    status: "interviewing",
    revision: 1,
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
    ...overrides,
  };
}

describe("Skill Builder durable Run contracts", () => {
  test("keeps legacy synchronous session responses valid", () => {
    const response = {
      data: session(),
      request_id: "request-1",
    };

    expect(skillBuilderSessionResponseSchema.parse(response)).toEqual(response);
    expect(skillBuilderTurnResponseSchema.parse(response)).toEqual(response);
  });

  test("accepts a strict Run admission and rolling activeRun session field", () => {
    const admission = {
      runId: RUN_ID,
      status: "pending" as const,
      streamUrl: `/api/projects/${PROJECT_ID}/skill-builder/runs/${RUN_ID}/stream`,
    };

    expect(skillBuilderTurnResponseSchema.parse(admission)).toEqual(admission);
    expect(isSkillBuilderRunAdmission(admission)).toBe(true);
    expect(
      skillBuilderSessionResponseSchema.parse({
        data: session({ activeRun: admission }),
        request_id: "request-2",
      }).data.activeRun,
    ).toEqual(admission);
  });

  test("rejects terminal admissions and unknown response fields", () => {
    expect(
      skillBuilderTurnResponseSchema.safeParse({
        runId: RUN_ID,
        status: "success",
        streamUrl: "/api/stream",
      }).success,
    ).toBe(false);
    expect(
      skillBuilderTurnResponseSchema.safeParse({
        runId: RUN_ID,
        status: "running",
        streamUrl: "/api/stream",
        owner_user_id: "must-not-leak",
      }).success,
    ).toBe(false);
  });

  test("accepts only non-authorizing exact dependency snapshots", () => {
    const dependency = {
      version: 1 as const,
      draft_checksum: "a".repeat(64),
      requirements: [
        {
          kind: "mcp_tool" as const,
          reference: "mcp:project:docs:v1:search_docs",
          scope: "project" as const,
          mcp_server_id: "55555555-5555-4555-8555-555555555555",
          version_id: "66666666-6666-4666-8666-666666666666",
          version_number: 1,
          server_slug: "docs",
          server_name: "Docs",
          tool_name: "search_docs",
          payload_checksum: "b".repeat(64),
          inventory_status: "ready" as const,
          inventory_error_code: null,
          last_success_at: NOW,
          authoring_only: true as const,
          runtime_authorized: false as const,
        },
      ],
    };
    const response = {
      data: session({ authoring_dependencies: dependency }),
      request_id: "request-dependencies",
    };

    expect(
      skillBuilderSessionResponseSchema.parse(response).data
        .authoring_dependencies,
    ).toEqual(dependency);
    expect(
      skillBuilderSessionResponseSchema.safeParse({
        ...response,
        data: {
          ...response.data,
          authoring_dependencies: {
            ...dependency,
            requirements: [
              {
                ...dependency.requirements[0],
                runtime_authorized: true,
              },
            ],
          },
        },
      }).success,
    ).toBe(false);
  });

  test("accepts only the secret-free stream projection", () => {
    const projection = {
      runId: RUN_ID,
      status: "running" as const,
      messages: [
        {
          id: "message-1",
          role: "assistant" as const,
          content: "正在读取项目能力目录",
          created_at: NOW,
        },
      ],
      toolSteps: [
        {
          id: "step-1",
          toolName: "project_asset_catalog",
          status: "completed" as const,
        },
      ],
      clarification: null,
    };

    expect(skillBuilderRunStreamProjectionSchema.parse(projection)).toEqual(
      projection,
    );
    expect(
      skillBuilderRunStreamProjectionSchema.safeParse({
        ...projection,
        toolSteps: [
          {
            ...projection.toolSteps[0],
            rawArguments: { credential: "must-not-render" },
          },
        ],
      }).success,
    ).toBe(false);
  });

  test("polls while a durable Run is active even before legacy status changes", () => {
    const active = session({
      activeRun: {
        runId: RUN_ID,
        status: "running",
        streamUrl: "/api/stream",
      },
    });

    expect(skillBuilderPollingInterval(active)).toBe(1_000);
    expect(skillBuilderPollingInterval(session())).toBe(false);
  });

  test("uses the durable session to finish a previously observed Run", () => {
    expect(
      skillBuilderRunPresentation(session({ status: "draft_ready" }), RUN_ID),
    ).toEqual({ runId: RUN_ID, status: "success" });
    expect(
      skillBuilderRunPresentation(
        session({
          status: "failed",
          error_code: "SKILL_DESIGN_GENERATION_INTERRUPTED",
        }),
        RUN_ID,
      ),
    ).toEqual({ runId: RUN_ID, status: "interrupted" });
    expect(
      skillBuilderRunPresentation(session({ status: "cancelled" }), RUN_ID),
    ).toEqual({ runId: RUN_ID, status: "cancelled" });
  });

  test("does not fabricate a terminal Run for an unobserved session", () => {
    expect(
      skillBuilderRunPresentation(session({ status: "draft_ready" }), null),
    ).toBeNull();
  });
});
