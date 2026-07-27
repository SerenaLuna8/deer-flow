import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderBlueprintSchema,
  agentBuilderSessionResponseSchema,
  agentBuilderSessionSchema,
} from "@/core/agent-builder/types";

const sessionId = "11111111-1111-4111-8111-111111111111";
const projectId = "22222222-2222-4222-8222-222222222222";
const threadId = "33333333-3333-4333-8333-333333333333";
const assetId = "44444444-4444-4444-8444-444444444444";

const blueprint = {
  description: "负责生成可运行且边界清晰的测试。",
  model_ref: "default",
  tool_groups: ["files"],
  skill_version_ids: [],
  mcp_version_ids: [],
  agents_instructions: "# 工作方式\n\n先理解目标，再生成测试。",
  soul: "# 风格\n\n务实、直接、少废话。",
  identity: "# 身份\n\n你是一名测试工程师。",
  user_context: "# 用户\n\n用户偏好可运行的结果。",
};

const session = {
  id: sessionId,
  project_id: projectId,
  owner_user_id: "user-1",
  thread_id: threadId,
  slug: "test-engineer",
  display_name: "test-engineer",
  status: "proposal_ready",
  revision: 4,
  blueprint,
  blueprint_checksum: "checksum-1",
  messages: [
    {
      id: "message-1",
      role: "assistant",
      content: "我已经整理好第一版设计。",
      created_at: "2026-07-26T08:00:00Z",
    },
  ],
  active_clarification: null,
  progress: [
    {
      id: "project-context",
      label: "了解项目现有 Agent",
      status: "completed",
    },
    {
      id: "generate-blueprint",
      label: "生成四项 Agent 设置",
      status: "completed",
    },
  ],
  error_code: null,
  error_message: null,
  created_agent_id: null,
  created_at: "2026-07-26T07:58:00Z",
  updated_at: "2026-07-26T08:00:00Z",
};

describe("agent builder contracts", () => {
  test("accepts a complete four-document blueprint", () => {
    expect(agentBuilderBlueprintSchema.parse(blueprint)).toEqual(blueprint);
  });

  test("rejects an incomplete or unknown blueprint payload", () => {
    expect(() =>
      agentBuilderBlueprintSchema.parse({
        ...blueprint,
        identity: undefined,
      }),
    ).toThrow();
    expect(() =>
      agentBuilderBlueprintSchema.parse({
        ...blueprint,
        unexpected: "private server field",
      }),
    ).toThrow();
  });

  test("strictly parses a resumable proposal-ready session", () => {
    expect(agentBuilderSessionSchema.parse(session)).toEqual(session);
    expect(
      agentBuilderSessionResponseSchema.parse({
        data: session,
        request_id: "request-1",
      }).data.id,
    ).toBe(sessionId);
  });

  test("rejects unknown authority and malformed completion fields", () => {
    expect(() =>
      agentBuilderSessionSchema.parse({
        ...session,
        internal_prompt: "must never reach the browser",
      }),
    ).toThrow();
    expect(() =>
      agentBuilderSessionSchema.parse({
        ...session,
        status: "completed",
        created_agent_id: "not-a-uuid",
      }),
    ).toThrow();
    expect(
      agentBuilderSessionSchema.parse({
        ...session,
        status: "completed",
        created_agent_id: assetId,
      }).created_agent_id,
    ).toBe(assetId);
    expect(() =>
      agentBuilderSessionSchema.parse({
        ...session,
        active_clarification: {
          version: 1,
          kind: "human_input_request",
          source: "ask_clarification",
          request_id: "clarification-1",
          question: "选择一个方向",
          input_mode: "free_text",
          internal_prompt: "must never reach the browser",
        },
      }),
    ).toThrow();
  });
});
