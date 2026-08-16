import { describe, expect, test } from "@rstest/core";

import {
  adminJobSchema,
  jobFiltersSchema,
} from "@/core/admin-operations/types";
import {
  adminModelItemSchema,
  createAdminModelInputSchema,
  replaceAdminModelInputSchema,
  testAdminModelConnectionInputSchema,
} from "@/core/admin-settings/models/types";
import {
  agentRuntimeSettingsValueSchema,
  validateAgentRuntimeModelReferences,
} from "@/core/admin-settings/system/types";
import { modelSchema } from "@/core/models/types";
import { auditItemSchema } from "@/core/project-governance/audit";

const JOB_TYPES = [
  "private_run",
  "automation_run",
  "retention_purge",
  "mcp_discovery",
  "memory_dream",
  "memory_seal",
] as const;
const PUBLIC_MODEL_ID = "00000000-0000-4000-8000-000000000201";
const VISION_MODEL_ID = "00000000-0000-4000-8000-000000000202";

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
  test("rejects removed descriptions from the public model contract", () => {
    const model = {
      name: PUBLIC_MODEL_ID,
      model: PUBLIC_MODEL_ID,
      display_name: "Visible model name",
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: false,
      supports_vision_bridge: false,
      is_default: true,
    };

    expect(modelSchema.safeParse(model).success).toBe(true);
    expect(
      modelSchema.safeParse({
        ...model,
        name: "legacy-logical-name",
        model: "legacy-logical-name",
      }).success,
    ).toBe(false);
    expect(
      modelSchema.safeParse({ ...model, description: "Removed" }).success,
    ).toBe(false);
  });

  test("rejects removed fields from admin model catalog items", () => {
    const item = {
      id: "00000000-0000-4000-8000-000000000208",
      display_name: "Visible admin model",
      provider_adapter: "vision_bridge_fake" as const,
      provider_model: "vision-bridge-fake-v1",
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: true,
      credential_id: null,
      credential_version_id: null,
      credential_env_key: null,
      status: "suspended" as const,
      is_default: false,
      revision: 1,
      version_number: 1,
      updated_at: "2026-08-16T00:00:00+00:00",
    };

    expect(adminModelItemSchema.safeParse(item).success).toBe(true);
    for (const removedField of [
      { logical_name: "legacy-logical-name" },
      { description: "Removed" },
      { sort_order: 0 },
    ]) {
      expect(
        adminModelItemSchema.safeParse({
          ...item,
          ...removedField,
        }).success,
      ).toBe(false);
    }
  });

  test("reads retired model adapters but rejects them from every write contract", () => {
    const retiredAdapters = [
      "patched_mimo",
      "patched_minimax",
      "patched_stepfun",
      "mindie",
      "claude_code",
      "codex_cli",
    ] as const;
    const base = {
      display_name: "Retired model",
      provider_model: "retired-model",
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: false,
      credential_id: null,
      credential_version_id: null,
      credential_env_key: null,
    };

    for (const provider_adapter of retiredAdapters) {
      const credentialRequired = !["claude_code", "codex_cli"].includes(
        provider_adapter,
      );
      const item = {
        ...base,
        provider_adapter,
        ...(credentialRequired
          ? {
              credential_id: "00000000-0000-4000-8000-000000000103",
              credential_version_id: "00000000-0000-4000-8000-000000000104",
              credential_env_key: "LEGACY_API_KEY",
            }
          : {}),
      };

      expect(
        adminModelItemSchema.safeParse({
          ...item,
          id: "00000000-0000-4000-8000-000000000105",
          status: "suspended",
          is_default: false,
          revision: 1,
          version_number: 1,
          updated_at: "2026-08-16T00:00:00+00:00",
        }).success,
      ).toBe(true);
      expect(
        createAdminModelInputSchema.safeParse({
          ...item,
          status: "suspended",
        }).success,
      ).toBe(false);
      expect(
        replaceAdminModelInputSchema.safeParse({
          ...item,
          expected_revision: 1,
        }).success,
      ).toBe(false);
      expect(
        testAdminModelConnectionInputSchema.safeParse({
          provider_adapter,
          provider_model: item.provider_model,
          settings: item.settings,
          supports_vision: false,
          credential_id: item.credential_id,
          credential_version_id: item.credential_version_id,
          credential_env_key: item.credential_env_key,
        }).success,
      ).toBe(false);
    }
  });

  test("accepts the credential-free Vision Bridge fake adapter", () => {
    const input = {
      display_name: "Small Vision Model",
      provider_adapter: "vision_bridge_fake",
      provider_model: "vision-bridge-fake-v1",
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: true,
      credential_id: null,
      credential_version_id: null,
      credential_env_key: null,
      status: "active",
    };
    expect(createAdminModelInputSchema.parse(input).provider_adapter).toBe(
      "vision_bridge_fake",
    );
    for (const removedField of [
      { logical_name: "internal-name" },
      { description: "Removed" },
      { sort_order: 0 },
    ]) {
      expect(
        createAdminModelInputSchema.safeParse({
          ...input,
          ...removedField,
        }).success,
      ).toBe(false);
    }
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        settings: { base_url: "https://example.com" },
      }).success,
    ).toBe(false);
  });

  test("accepts only the narrow credential-bound real Vision Bridge adapter", () => {
    const input = {
      display_name: "Small Vision API",
      provider_adapter: "vision_openai_compatible_v1",
      provider_model: "small-vlm",
      settings: { base_url: "https://vision.example.test/v1" },
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: true,
      credential_id: "00000000-0000-4000-8000-000000000101",
      credential_version_id: "00000000-0000-4000-8000-000000000102",
      credential_env_key: "VISION_API_KEY",
      status: "active",
    };

    expect(createAdminModelInputSchema.parse(input).provider_adapter).toBe(
      "vision_openai_compatible_v1",
    );
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        settings: { base_url: "http://vision.example.test/v1" },
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        settings: {
          base_url: "https://vision.example.test/v1",
          max_retries: 4,
        },
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        credential_id: null,
        credential_version_id: null,
        credential_env_key: null,
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
        model_name: VISION_MODEL_ID,
      },
    };
    expect(
      validateAgentRuntimeModelReferences(
        configured,
        [VISION_MODEL_ID],
        [VISION_MODEL_ID],
      ).vision_bridge.model_name,
    ).toBe(VISION_MODEL_ID);
    expect(() =>
      validateAgentRuntimeModelReferences(configured, [VISION_MODEL_ID], []),
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
