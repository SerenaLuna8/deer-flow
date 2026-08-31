import { describe, expect, test } from "@rstest/core";

import {
  adminJobSchema,
  jobFiltersSchema,
  operationsOverviewSchema,
} from "@/core/admin-operations/types";
import { testAdminModelProviderConnectionInputSchema } from "@/core/admin-settings/model-registry/types";
import {
  adminModelCatalogSchema,
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
const MODEL_PROVIDER_ID = "00000000-0000-4000-8000-00000000c001";
const MODEL_PROVIDER_NAME = "Contract Provider";

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
      trigger_tokens: null,
      keep: { type: "tokens", value: 64_000 },
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
      identical_calls: {
        warn_threshold: 3,
        hard_limit: 5,
        window_size: 20,
      },
    },
    internal_tool_call_limits: {
      lead_per_run: 200,
      subagent_per_task: 50,
    },
    read_before_write: { enabled: true },
    safety_finish_reason: { enabled: true },
    subagents: {
      max_concurrent: 3,
      max_total_per_run_by_workload: {
        interactive: 6,
        research: 9,
      },
    },
  };
}

describe("admin contracts", () => {
  test("accepts the redacted private-run fleet and queue aggregate contract", () => {
    const payload = {
      readiness: {
        status: "ready",
        database: "ready",
        schema: "ready",
        schema_state: "ready",
        worker_fleet: "ready",
        scheduler: "disabled",
        stream: "ready",
        quota: "ready",
        audit: "ready",
        role: "gateway",
        worker_count: 4,
        worker_capacity: 12,
        worker_oldest_heartbeat_age_seconds: 8,
        private_run_worker_fleet: "ready",
        private_run_worker_count: 2,
        private_run_worker_capacity: 7,
        scheduler_ownership: "disabled",
        run_skill_writer_mode: "legacy_v3",
        run_skill_writer_artifact_version: "run-skill-snapshot-writer-v2",
        run_skill_legacy_policy_digest:
          "e01a816a3f20a4ecf088e2f0d37b92ba16634e5969860b900a14924312edb6e8",
        run_skill_writer_ready: true,
        knowledge: "disabled",
      },
      data_status: "available",
      counts: {
        projects: 3,
        suspended_projects: 1,
        queued_jobs: 5,
        running_jobs: 2,
        dead_jobs: 1,
        ready_jobs: 4,
        oldest_ready_job_age_seconds: 17,
        stale_leases: 2,
        waiting_for_worker_runs: 3,
        waiting_for_terminalization_runs: 1,
      },
      usage: [
        { dimension: "members", used: 1, reserved: 0 },
        { dimension: "storage_bytes", used: 2, reserved: 0 },
        { dimension: "concurrent_runs", used: 3, reserved: 0 },
        { dimension: "mcp_calls_daily", used: 4, reserved: 0 },
      ],
      channel_providers: [],
    } as const;

    const parsed = operationsOverviewSchema.safeParse(payload);

    expect(parsed.success).toBe(true);
    expect(
      operationsOverviewSchema.safeParse({
        ...payload,
        readiness: {
          ...payload.readiness,
          private_run_worker_id: "worker-secret",
        },
      }).success,
    ).toBe(false);
    expect(
      operationsOverviewSchema.safeParse({
        ...payload,
        counts: {
          ...payload.counts,
          execution_domain_affinity: "affinity-secret",
        },
      }).success,
    ).toBe(false);
  });

  test("accepts the complete schema v6 Agent runtime policy with independent Lead and Sub-Agent Task limits", () => {
    const parsed = agentRuntimeSettingsValueSchema.parse(
      agentRuntimeSettings(),
    );

    expect(parsed.loop_detection.identical_calls).toEqual({
      warn_threshold: 3,
      hard_limit: 5,
      window_size: 20,
    });
    expect(parsed.internal_tool_call_limits).toEqual({
      lead_per_run: 200,
      subagent_per_task: 50,
    });
    expect(parsed.subagents.max_total_per_run_by_workload).toEqual({
      interactive: 6,
      research: 9,
    });
  });

  test("fails closed on incomplete, malformed, wrapped, or legacy Agent runtime policy shapes", () => {
    const missingLimit = structuredClone(agentRuntimeSettings()) as Record<
      string,
      unknown
    >;
    delete missingLimit.internal_tool_call_limits;
    expect(
      agentRuntimeSettingsValueSchema.safeParse(missingLimit).success,
    ).toBe(false);

    const invalidLimit = structuredClone(agentRuntimeSettings());
    invalidLimit.internal_tool_call_limits.lead_per_run = 0;
    expect(
      agentRuntimeSettingsValueSchema.safeParse(invalidLimit).success,
    ).toBe(false);

    const legacySharedLimit = {
      ...agentRuntimeSettings(),
      internal_tool_call_limit: 200,
    } as Record<string, unknown>;
    delete legacySharedLimit.internal_tool_call_limits;
    expect(
      agentRuntimeSettingsValueSchema.safeParse(legacySharedLimit).success,
    ).toBe(false);

    const legacy = structuredClone(agentRuntimeSettings()) as Record<
      string,
      unknown
    >;
    legacy.loop_detection = {
      enabled: true,
      warn_threshold: 3,
      hard_limit: 5,
      window_size: 20,
      max_tracked_threads: 100,
      tool_freq_warn: 6,
      tool_freq_hard_limit: 10,
      tool_freq_overrides: {},
    };
    expect(agentRuntimeSettingsValueSchema.safeParse(legacy).success).toBe(
      false,
    );
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        agent_runtime: agentRuntimeSettings(),
      }).success,
    ).toBe(false);

    for (const keep of [
      { type: "messages", value: 20 },
      { type: "fraction", value: 0.8 },
    ] as const) {
      const legacyKeep = structuredClone(agentRuntimeSettings());
      legacyKeep.summarization.keep = keep;
      expect(
        agentRuntimeSettingsValueSchema.safeParse(legacyKeep).success,
      ).toBe(false);
    }
  });

  test("rejects the removed per-tool budget contract", () => {
    const policy = {
      ...agentRuntimeSettings(),
      tool_call_budget: {
        profiles: {
          interactive: {
            lead: {
              default: { warn: 30, hard_limit: 50 },
              tools: { web_search: { warn: 6, hard_limit: 10 } },
            },
          },
        },
      },
    };

    const parsed = agentRuntimeSettingsValueSchema.safeParse(policy);

    expect(parsed.success).toBe(false);
  });

  test("rejects a repeated-call window smaller than its hard limit", () => {
    const policy = agentRuntimeSettings();
    policy.loop_detection.identical_calls.window_size = 4;

    const parsed = agentRuntimeSettingsValueSchema.safeParse(policy);

    expect(parsed.success).toBe(false);
    if (parsed.success) return;
    expect(parsed.error.issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          path: ["loop_detection", "identical_calls", "window_size"],
          message: "Window size cannot be below the hard limit",
        }),
      ]),
    );
  });

  test("fails closed on malformed provider descriptors while allowing intentional omission", () => {
    const baseUrlField = {
      name: "base_url",
      label: "Base URL",
      input_type: "url" as const,
      advanced: false,
      form_control: "input" as const,
      default_mode: "platform" as const,
      default_value: "https://api.example.test/v1",
      minimum: null,
      maximum: null,
      step: null,
      options: [],
    };
    const customNumberField = {
      name: "vendor_quality",
      label: "Vendor quality",
      input_type: "integer" as const,
      advanced: false,
      form_control: "input" as const,
      default_mode: "platform" as const,
      default_value: 4,
      minimum: 1,
      maximum: 7,
      step: 1,
      options: [],
    };
    const newAdapterDescriptor = {
      id: "new_vendor_v2",
      api_key_required: false,
      setting_fields: [baseUrlField, customNumberField],
    };
    const catalog = {
      items: [],
      provider_adapters: [newAdapterDescriptor],
      catalog_revision: 1,
      request_id: "descriptor-contract",
    };

    expect(
      adminModelCatalogSchema
        .parse(catalog)
        .provider_adapters.map((descriptor) => descriptor.id),
    ).toEqual(["new_vendor_v2"]);
    const { provider_adapters: _providerAdapters, ...missingDescriptors } =
      catalog;
    expect(_providerAdapters).toHaveLength(1);
    expect(adminModelCatalogSchema.safeParse(missingDescriptors).success).toBe(
      false,
    );
    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        provider_adapters: [
          { ...newAdapterDescriptor, id: "Unknown-Provider" },
        ],
      }).success,
    ).toBe(false);
    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        provider_adapters: [newAdapterDescriptor, newAdapterDescriptor],
      }).success,
    ).toBe(false);
    const unsupportedField = adminModelCatalogSchema.safeParse({
      ...catalog,
      provider_adapters: [
        {
          ...newAdapterDescriptor,
          setting_fields: [
            {
              ...customNumberField,
              input_type: "vendor_magic_mode",
            },
          ],
        },
      ],
    });
    expect(unsupportedField.success).toBe(false);
    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        provider_adapters: [
          {
            ...newAdapterDescriptor,
            setting_fields: [
              { ...customNumberField, name: "vendor_quality" },
              { ...customNumberField, name: "vendor_quality" },
            ],
          },
        ],
      }).success,
    ).toBe(false);
    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        provider_adapters: [
          {
            ...newAdapterDescriptor,
            setting_fields: [
              {
                ...baseUrlField,
                minimum: 1,
              },
            ],
          },
        ],
      }).success,
    ).toBe(false);

    for (const invalidDefault of [
      { ...customNumberField, default_value: null },
      { ...customNumberField, default_value: 4.5 },
      { ...customNumberField, default_value: 8 },
      {
        ...customNumberField,
        default_mode: "provider" as const,
        default_value: 4,
      },
      {
        ...customNumberField,
        form_control: "preserve" as const,
        input_type: "json" as const,
        advanced: false,
        default_mode: "provider" as const,
        default_value: null,
      },
    ]) {
      expect(
        adminModelCatalogSchema.safeParse({
          ...catalog,
          provider_adapters: [
            { ...newAdapterDescriptor, setting_fields: [invalidDefault] },
          ],
        }).success,
      ).toBe(false);
    }
  });

  test("keeps the legacy vision flag readable but optional in the public model contract", () => {
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

    expect(modelSchema.parse(model).supports_vision_bridge).toBe(false);
    const { supports_vision_bridge: _legacyVisionFlag, ...withoutLegacyFlag } =
      model;
    expect(_legacyVisionFlag).toBe(false);
    expect(modelSchema.parse(withoutLegacyFlag).supports_vision_bridge).toBe(
      false,
    );
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
      provider_id: MODEL_PROVIDER_ID,
      provider_name: MODEL_PROVIDER_NAME,
      provider_adapter: "historical_adapter_v1" as const,
      provider_model: "historical-model-v1",
      max_input_tokens: 128_000,
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: true,
      status: "suspended" as const,
      is_default: false,
      revision: 1,
      api_key_configured: false,
      secret_readiness: "unready" as const,
      secret_revision: 0,
      updated_at: "2026-08-16T00:00:00+00:00",
    };

    expect(adminModelItemSchema.safeParse(item).success).toBe(true);
    for (const removedField of [
      { logical_name: "legacy-logical-name" },
      { description: "Removed" },
      { sort_order: 0 },
      { api_key: "temporary-key" },
    ]) {
      expect(
        adminModelItemSchema.safeParse({
          ...item,
          ...removedField,
        }).success,
      ).toBe(false);
    }
    // The provider binding is part of the read contract, not optional data.
    const { provider_id: _providerId, ...withoutProviderId } = item;
    expect(_providerId).toBe(MODEL_PROVIDER_ID);
    expect(adminModelItemSchema.safeParse(withoutProviderId).success).toBe(
      false,
    );
    const { provider_name: _providerName, ...withoutProviderName } = item;
    expect(_providerName).toBe(MODEL_PROVIDER_NAME);
    expect(adminModelItemSchema.safeParse(withoutProviderName).success).toBe(
      false,
    );
  });

  test("requires one bounded maximum input context across model read and write contracts", () => {
    const versionWithoutCapacity = {
      display_name: "Context-bounded model",
      provider_adapter: "openai" as const,
      provider_model: "gpt-context-bounded",
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: true,
    };
    const itemWithoutCapacity = {
      ...versionWithoutCapacity,
      id: "00000000-0000-4000-8000-000000000209",
      provider_id: MODEL_PROVIDER_ID,
      provider_name: MODEL_PROVIDER_NAME,
      status: "active" as const,
      is_default: false,
      revision: 1,
      api_key_configured: true,
      secret_readiness: "ready" as const,
      secret_revision: 1,
      updated_at: "2026-08-23T00:00:00+00:00",
    };

    expect(adminModelItemSchema.safeParse(itemWithoutCapacity).success).toBe(
      false,
    );
    for (const maxInputTokens of [1, 200_000, 2_000_000]) {
      const version = {
        ...versionWithoutCapacity,
        max_input_tokens: maxInputTokens,
      };
      expect(
        adminModelItemSchema.safeParse({
          ...itemWithoutCapacity,
          max_input_tokens: maxInputTokens,
        }).success,
      ).toBe(true);
      expect(
        createAdminModelInputSchema.safeParse({
          ...version,
          status: "active",
          provider_id: MODEL_PROVIDER_ID,
        }).success,
      ).toBe(true);
      expect(
        replaceAdminModelInputSchema.safeParse({
          ...version,
          provider_id: MODEL_PROVIDER_ID,
        }).success,
      ).toBe(true);
      expect(
        testAdminModelConnectionInputSchema.safeParse({
          provider_id: MODEL_PROVIDER_ID,
          provider_adapter: version.provider_adapter,
          provider_model: version.provider_model,
          settings: version.settings,
          max_input_tokens: version.max_input_tokens,
          supports_vision: version.supports_vision,
        }).success,
      ).toBe(true);
    }
    for (const invalidCapacity of [0, -1, 1.5, 2_000_001]) {
      expect(
        adminModelItemSchema.safeParse({
          ...itemWithoutCapacity,
          max_input_tokens: invalidCapacity,
        }).success,
      ).toBe(false);
    }
  });

  test("reads the historical retry field but rejects it from every write contract", () => {
    const version = {
      display_name: "Historical retry model",
      provider_adapter: "openai" as const,
      provider_model: "gpt-history",
      max_input_tokens: 128_000,
      settings: { max_retries: 4 },
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: false,
    };

    expect(
      adminModelItemSchema.safeParse({
        ...version,
        id: "00000000-0000-4000-8000-000000000103",
        provider_id: MODEL_PROVIDER_ID,
        provider_name: MODEL_PROVIDER_NAME,
        status: "suspended",
        is_default: false,
        revision: 1,
        api_key_configured: true,
        secret_readiness: "ready",
        secret_revision: 1,
        updated_at: "2026-08-16T00:00:00+00:00",
      }).success,
    ).toBe(true);
    expect(
      createAdminModelInputSchema.safeParse({
        ...version,
        status: "suspended",
        provider_id: MODEL_PROVIDER_ID,
      }).success,
    ).toBe(false);
    expect(
      replaceAdminModelInputSchema.safeParse({
        ...version,
        provider_id: MODEL_PROVIDER_ID,
        expected_revision: 1,
      }).success,
    ).toBe(false);
    expect(
      testAdminModelConnectionInputSchema.safeParse({
        provider_id: MODEL_PROVIDER_ID,
        provider_adapter: version.provider_adapter,
        provider_model: version.provider_model,
        max_input_tokens: version.max_input_tokens,
        settings: version.settings,
        supports_vision: version.supports_vision,
      }).success,
    ).toBe(false);
  });

  test("model write contracts bind a provider and carry no model-level key", () => {
    const version = {
      display_name: "Provider-bound model",
      provider_adapter: "openai" as const,
      provider_model: "gpt-provider-bound",
      max_input_tokens: 128_000,
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: false,
    };

    // A model never carries its own key; the strict contracts reject one.
    for (const keyed of [
      createAdminModelInputSchema.safeParse({
        ...version,
        status: "active",
        provider_id: MODEL_PROVIDER_ID,
        api_key: "temporary-key",
      }),
      replaceAdminModelInputSchema.safeParse({
        ...version,
        provider_id: MODEL_PROVIDER_ID,
        api_key: null,
      }),
      testAdminModelConnectionInputSchema.safeParse({
        provider_id: MODEL_PROVIDER_ID,
        provider_adapter: version.provider_adapter,
        provider_model: version.provider_model,
        max_input_tokens: version.max_input_tokens,
        settings: version.settings,
        supports_vision: version.supports_vision,
        api_key: "temporary-key",
      }),
    ]) {
      expect(keyed.success).toBe(false);
    }
    // The provider binding itself is required.
    expect(
      createAdminModelInputSchema.safeParse({
        ...version,
        status: "active",
      }).success,
    ).toBe(false);
    expect(replaceAdminModelInputSchema.safeParse(version).success).toBe(false);
  });

  test("candidate provider connection test carries a transient URL and key", () => {
    const candidate = {
      base_url: "https://candidate.example.test/v1",
      api_key: "candidate-key",
      provider_adapter: "openai" as const,
      provider_model: "gpt-candidate",
      max_input_tokens: 128_000,
      settings: {},
      supports_vision: false,
    };

    expect(
      testAdminModelProviderConnectionInputSchema.safeParse(candidate).success,
    ).toBe(true);
    const { api_key: _candidateKey, ...withoutKey } = candidate;
    expect(_candidateKey).toBe("candidate-key");
    expect(
      testAdminModelProviderConnectionInputSchema.safeParse(withoutKey).success,
    ).toBe(false);
    expect(
      testAdminModelProviderConnectionInputSchema.safeParse({
        ...candidate,
        api_key: "",
      }).success,
    ).toBe(false);
    expect(
      testAdminModelProviderConnectionInputSchema.safeParse({
        ...candidate,
        base_url: "https://candidate.example.test/v1?token=leak",
      }).success,
    ).toBe(false);
    expect(
      testAdminModelProviderConnectionInputSchema.safeParse({
        ...candidate,
        provider_id: MODEL_PROVIDER_ID,
      }).success,
    ).toBe(false);
  });

  test("reads bounded historical adapter IDs without maintaining a retired list", () => {
    const item = {
      id: "00000000-0000-4000-8000-000000000105",
      display_name: "Historical model",
      provider_id: MODEL_PROVIDER_ID,
      provider_name: MODEL_PROVIDER_NAME,
      provider_adapter: "legacy_adapter_v1",
      provider_model: "legacy-model",
      max_input_tokens: 128_000,
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: false,
      status: "suspended",
      is_default: false,
      revision: 1,
      api_key_configured: false,
      secret_readiness: "unready",
      secret_revision: 0,
      updated_at: "2026-08-16T00:00:00+00:00",
    };

    expect(adminModelItemSchema.safeParse(item).success).toBe(true);
    for (const invalidAdapter of [
      "LegacyAdapter",
      "legacy-adapter",
      `a${"b".repeat(64)}`,
    ]) {
      expect(
        adminModelItemSchema.safeParse({
          ...item,
          provider_adapter: invalidAdapter,
        }).success,
      ).toBe(false);
    }
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
