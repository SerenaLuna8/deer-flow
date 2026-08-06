import { describe, expect, test } from "@rstest/core";

import {
  adminJobSchema,
  jobFiltersSchema,
} from "@/core/admin-operations/types";
import { agentRuntimeSettingsValueSchema } from "@/core/admin-settings/system/types";
import { auditItemSchema } from "@/core/project-governance/audit";

const JOB_TYPES = [
  "private_run",
  "automation_run",
  "retention_purge",
  "mcp_discovery",
  "memory_dream",
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
  test("accepts only the final frozen Memory policy", () => {
    const parsed = agentRuntimeSettingsValueSchema.parse(
      agentRuntimeSettings(),
    );

    expect(parsed.memory).toMatchObject({
      enabled: true,
      model_name: null,
      dream_interval_minutes: 120,
      max_injection_tokens: 2_000,
    });
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeSettings(),
        memory: { ...agentRuntimeSettings().memory, unexpected: true },
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
