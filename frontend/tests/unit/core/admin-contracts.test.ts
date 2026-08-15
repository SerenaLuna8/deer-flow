import { describe, expect, test } from "@rstest/core";

import {
  adminJobSchema,
  jobFiltersSchema,
} from "@/core/admin-operations/types";
import { createAdminModelInputSchema } from "@/core/admin-settings/models/types";
import {
  agentRuntimeSettingsValueSchema,
  validateAgentRuntimeModelReferences,
} from "@/core/admin-settings/system/types";
import { auditItemSchema } from "@/core/project-governance/audit";

const JOB_TYPES = [
  "private_run",
  "automation_run",
  "retention_purge",
  "mcp_discovery",
  "memory_dream",
  "memory_seal",
] as const;

function agentRuntimeSettings() {
  return {
    token_usage: { enabled: true },
    token_budget: {
      enabled: true,
      max_tokens: 100_000,
      max_input_tokens: null,
      max_output_tokens: null,
      warn_threshold: 0.8,
      hard_stop_threshold: 0.95,
    },
    max_recursion_limit: 100,
    vision_bridge: {
      model_name: null,
      timeout_seconds: 20,
      contract_version: "vision.bridge.v1",
    },
    title: {
      enabled: true,
      max_words: 10,
      max_chars: 80,
      model_name: null,
    },
    suggestions: { enabled: true },
    input_polish: { enabled: true, max_chars: 10_000, model_name: null },
    summarization: {
      enabled: true,
      model_name: null,
      trigger: null,
      keep: { type: "messages", value: 20 },
      trim_tokens_to_summarize: null,
      skill_file_read_tool_names: ["read_file"],
    },
    memory: {
      enabled: true,
      model_name: null,
      dream_interval_minutes: 120,
      max_injection_tokens: 2_000,
      idle_seal_minutes: 1_440,
      episode_retention_days: 365,
    },
    tool_search: { enabled: true, auto_promote_top_k: 3 },
    tool_output: {
      enabled: true,
      externalize_min_chars: 10_000,
      preview_head_chars: 1_000,
      preview_tail_chars: 1_000,
      fallback_max_chars: 10_000,
      fallback_head_chars: 1_000,
      fallback_tail_chars: 1_000,
      exempt_tools: [],
      tool_overrides: {},
    },
    loop_detection: {
      enabled: true,
      warn_threshold: 10,
      hard_limit: 20,
      window_size: 50,
      max_tracked_threads: 100,
      tool_freq_warn: 5,
      tool_freq_hard_limit: 10,
      tool_freq_overrides: {},
    },
    read_before_write: { enabled: true },
    safety_finish_reason: { enabled: true },
    subagents: { max_total_per_run: 5 },
  };
}

describe("admin contracts", () => {
  test("accepts only the credential-free P1 Vision Bridge fake adapter", () => {
    const input = {
      logical_name: "small-vision-model",
      display_name: "Small Vision Model",
      description: "P1 deterministic adapter",
      provider_adapter: "vision_bridge_fake",
      provider_model: "vision-bridge-fake-v1",
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: true,
      credential_id: null,
      credential_version_id: null,
      credential_env_key: null,
      sort_order: 0,
      status: "active",
    };
    expect(createAdminModelInputSchema.parse(input).provider_adapter).toBe(
      "vision_bridge_fake",
    );
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        settings: { base_url: "https://example.com" },
      }).success,
    ).toBe(false);
  });

  test("accepts only the final frozen Memory policy", () => {
    const parsed = agentRuntimeSettingsValueSchema.parse(
      agentRuntimeSettings(),
    );

    expect(parsed.memory).toMatchObject({
      enabled: true,
      model_name: null,
      dream_interval_minutes: 120,
      max_injection_tokens: 2_000,
      idle_seal_minutes: 1_440,
      episode_retention_days: 365,
    });
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeSettings(),
        memory: { ...agentRuntimeSettings().memory, unexpected: true },
      }).success,
    ).toBe(false);
    for (const invalid of [-1, 1, 29, 10_081]) {
      expect(
        agentRuntimeSettingsValueSchema.safeParse({
          ...agentRuntimeSettings(),
          memory: {
            ...agentRuntimeSettings().memory,
            idle_seal_minutes: invalid,
          },
        }).success,
      ).toBe(false);
    }
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeSettings(),
        memory: { ...agentRuntimeSettings().memory, idle_seal_minutes: 0 },
      }).success,
    ).toBe(true);
    for (const invalid of [-1, 1, 29, 3_651]) {
      expect(
        agentRuntimeSettingsValueSchema.safeParse({
          ...agentRuntimeSettings(),
          memory: {
            ...agentRuntimeSettings().memory,
            episode_retention_days: invalid,
          },
        }).success,
      ).toBe(false);
    }
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeSettings(),
        memory: { ...agentRuntimeSettings().memory, episode_retention_days: 0 },
      }).success,
    ).toBe(true);
  });

  test("keeps Vision Bridge optional, strict, and compatibility-gated", () => {
    const configured = {
      ...agentRuntimeSettings(),
      vision_bridge: {
        ...agentRuntimeSettings().vision_bridge,
        model_name: "small-vision-model",
      },
    };
    expect(
      validateAgentRuntimeModelReferences(
        configured,
        ["small-vision-model"],
        ["small-vision-model"],
      ).vision_bridge.model_name,
    ).toBe("small-vision-model");
    expect(() =>
      validateAgentRuntimeModelReferences(
        configured,
        ["small-vision-model"],
        [],
      ),
    ).toThrow("active compatible vision model");
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeSettings(),
        vision_bridge: {
          ...agentRuntimeSettings().vision_bridge,
          enabled: true,
        },
      }).success,
    ).toBe(false);
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeSettings(),
        vision_bridge: {
          ...agentRuntimeSettings().vision_bridge,
          egress_grant: true,
        },
      }).success,
    ).toBe(false);
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeSettings(),
        vision_bridge: {
          ...agentRuntimeSettings().vision_bridge,
          contract_version: "vision.bridge.v2",
        },
      }).success,
    ).toBe(false);
  });

  test("accepts every persisted job type in rows and filters", () => {
    for (const jobType of JOB_TYPES) {
      expect(
        adminJobSchema.parse({
          job_id: "11111111-1111-4111-8111-111111111111",
          dead_job_id: null,
          project_id: "22222222-2222-4222-8222-222222222222",
          project_slug: "alpha-project",
          project_display_name: "Alpha Project",
          job_type: jobType,
          status: "queued",
          retry_safety: "unknown",
          safe_to_requeue: false,
          public_error_code: null,
          predecessor_dead_job_id: null,
        }).job_type,
      ).toBe(jobType);
      expect(jobFiltersSchema.parse({ type: jobType }).type).toBe(jobType);
      expect(
        auditItemSchema.parse({
          id: "33333333-3333-4333-8333-333333333333",
          occurred_at: "2026-08-05T00:00:00Z",
          actor: "worker",
          action: "job.dead",
          target_kind: "job",
          outcome: "failed",
          public_error_code: "MEMORY_JOB_FAILED",
          metadata: {
            job_type: jobType,
            public_error_code: "MEMORY_JOB_FAILED",
            attempt_count: 1,
            retry_safety: "safe",
          },
        }).metadata.job_type,
      ).toBe(jobType);
    }
  });
});
