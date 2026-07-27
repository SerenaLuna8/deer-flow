import { describe, expect, test } from "@rstest/core";

import {
  agentBuilderBlueprintValidationError,
  agentBuilderCanAuthor,
  agentBuilderCanComplete,
  agentBuilderComposerDisabled,
  agentBuilderDisplayState,
  agentBuilderSlugError,
  normalizeAgentBuilderSlug,
} from "@/core/agent-builder/state";
import type { AgentBuilderSession } from "@/core/agent-builder/types";

const session = {
  id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  owner_user_id: "user-1",
  thread_id: "33333333-3333-4333-8333-333333333333",
  slug: "test-engineer",
  display_name: "test-engineer",
  status: "interviewing",
  revision: 1,
  blueprint: null,
  blueprint_checksum: null,
  messages: [],
  active_clarification: null,
  progress: [],
  error_code: null,
  error_message: null,
  created_agent_id: null,
  created_at: "2026-07-26T07:58:00Z",
  updated_at: "2026-07-26T07:58:00Z",
} satisfies AgentBuilderSession;

describe("agent builder state", () => {
  test("normalizes a visible Agent name into a stable project slug", () => {
    expect(normalizeAgentBuilderSlug(" Code Reviewer ")).toBe("code-reviewer");
    expect(normalizeAgentBuilderSlug("code---reviewer")).toBe("code-reviewer");
  });

  test("maps slug validation to actionable Chinese errors", () => {
    expect(agentBuilderSlugError("ab")).toBe("名称至少需要 3 个字符");
    expect(agentBuilderSlugError("Code Reviewer")).toBe(
      "仅支持小写字母、数字和单个连字符",
    );
    expect(agentBuilderSlugError("code-reviewer")).toBeNull();
  });

  test("keeps generation, clarification and commit states distinct", () => {
    expect(agentBuilderDisplayState(session)).toBe("interviewing");
    expect(agentBuilderDisplayState({ ...session, status: "generating" })).toBe(
      "generating",
    );
    expect(
      agentBuilderDisplayState({
        ...session,
        status: "interviewing",
        active_clarification: {
          version: 1,
          kind: "human_input_request",
          source: "ask_clarification",
          request_id: "clarification-1",
          question: "你希望它主要承担什么职责？",
          input_mode: "free_text",
        },
      }),
    ).toBe("awaiting_clarification");
  });

  test("disables normal composer while another response or mutation owns input", () => {
    expect(agentBuilderComposerDisabled(session, false)).toBe(false);
    expect(
      agentBuilderComposerDisabled({ ...session, status: "generating" }, false),
    ).toBe(true);
    expect(
      agentBuilderComposerDisabled(
        {
          ...session,
          active_clarification: {
            version: 1,
            kind: "human_input_request",
            source: "ask_clarification",
            request_id: "clarification-1",
            question: "选择一个方向",
            input_mode: "single_choice",
            options: [{ id: "one", label: "方向一", value: "方向一" }],
          },
        },
        false,
      ),
    ).toBe(true);
    expect(agentBuilderComposerDisabled(session, true)).toBe(true);
    expect(agentBuilderComposerDisabled(session, false, true)).toBe(true);
  });

  test("uses the backend Agent Builder authoring capability without unrelated Run permissions", () => {
    expect(agentBuilderCanAuthor(["shared_assets.edit"])).toBe(true);
    expect(
      agentBuilderCanAuthor(["private_work.create", "shared_assets.execute"]),
    ).toBe(false);
  });

  test("requires every logical document and runtime field before saving or committing", () => {
    const completeBlueprint = {
      description: "测试",
      model_ref: "default",
      tool_groups: ["files"],
      skill_version_ids: [],
      mcp_version_ids: [],
      agents_instructions: "# Agent",
      soul: "# Soul",
      identity: "# Identity",
      user_context: "# User",
    };

    expect(agentBuilderBlueprintValidationError(completeBlueprint)).toBeNull();
    expect(
      agentBuilderBlueprintValidationError({
        ...completeBlueprint,
        identity: "  ",
      }),
    ).toContain("IDENTITY.md");
  });

  test("allows final creation only after a validated proposal exists", () => {
    expect(agentBuilderCanComplete(session)).toBe(false);
    expect(
      agentBuilderCanComplete({
        ...session,
        status: "proposal_ready",
        blueprint: {
          description: "测试",
          model_ref: "default",
          tool_groups: ["files"],
          skill_version_ids: [],
          mcp_version_ids: [],
          agents_instructions: "# Agent",
          soul: "# Soul",
          identity: "# Identity",
          user_context: "# User",
        },
      }),
    ).toBe(true);
    expect(
      agentBuilderCanComplete({
        ...session,
        status: "proposal_ready",
        blueprint: {
          description: "测试",
          model_ref: "default",
          tool_groups: ["files"],
          skill_version_ids: [],
          mcp_version_ids: [],
          agents_instructions: "# Agent",
          soul: "# Soul",
          identity: "",
          user_context: "# User",
        },
      }),
    ).toBe(false);
  });
});
