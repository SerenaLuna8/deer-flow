import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import {
  abortAdminSystemSettingsAccount,
  adminSystemSettingsMutationKey,
  adminSystemSettingsQueryKey,
  agentRuntimeSettingsValueSchema,
  fetchAdminSystemSettings,
  replaceAdminSystemSettingsSection,
  runAbortableAdminSystemSettingsMutation,
  systemSettingsCatalogSchema,
  systemSettingsMutationResponseSchema,
  validateAgentRuntimeModelReferences,
} from "@/core/admin-settings/system";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

const mockedFetch = rs.mocked(fetchWithAuth);
const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";

const agentRuntimeValue = {
  token_usage: { enabled: true },
  token_budget: {
    enabled: false,
    max_tokens: 200_000,
    max_input_tokens: null,
    max_output_tokens: null,
    warn_threshold: 0.8,
    hard_stop_threshold: 1,
  },
  max_recursion_limit: 1_000,
  title: {
    enabled: true,
    max_words: 6,
    max_chars: 60,
    model_name: "analysis-pro",
  },
  suggestions: { enabled: true },
  input_polish: {
    enabled: true,
    max_chars: 4_000,
    model_name: null,
  },
  summarization: {
    enabled: true,
    model_name: "analysis-pro",
    trigger: [{ type: "tokens", value: 32_000 }],
    keep: { type: "messages", value: 10 },
    trim_tokens_to_summarize: 15_564,
    skill_file_read_tool_names: ["read_file", "read", "view", "cat"],
  },
  memory: {
    enabled: true,
    search_enabled: true,
    debounce_seconds: 30,
    model_name: null,
    max_facts: 100,
    fact_confidence_threshold: 0.7,
    injection_enabled: true,
    max_injection_tokens: 2_000,
    token_counting: "tiktoken",
    guaranteed_categories: ["correction"],
    guaranteed_token_budget: 500,
    staleness_review_enabled: true,
    staleness_age_days: 90,
    staleness_min_candidates: 3,
    staleness_max_removals_per_cycle: 10,
    staleness_protected_categories: ["correction"],
  },
  tool_search: { enabled: false, auto_promote_top_k: 3 },
  tool_output: {
    enabled: true,
    externalize_min_chars: 12_000,
    preview_head_chars: 2_000,
    preview_tail_chars: 1_000,
    fallback_max_chars: 30_000,
    fallback_head_chars: 8_000,
    fallback_tail_chars: 3_000,
    exempt_tools: ["read_file", "read_file_tool"],
    tool_overrides: { web_fetch: 20_000 },
  },
  loop_detection: {
    enabled: true,
    warn_threshold: 3,
    hard_limit: 5,
    window_size: 20,
    max_tracked_threads: 100,
    tool_freq_warn: 30,
    tool_freq_hard_limit: 50,
    tool_freq_overrides: {
      web_search: { warn: 6, hard_limit: 10 },
      web_fetch: { warn: 6, hard_limit: 10 },
    },
  },
  read_before_write: { enabled: true },
  safety_finish_reason: { enabled: true },
  subagents: { max_total_per_run: 6 },
} as const;

const catalog = {
  catalog_revision: 7,
  sections: {
    agent_runtime: {
      section: "agent_runtime",
      revision: 3,
      schema_version: 1,
      value: agentRuntimeValue,
      effect_scope: "new_requests_and_runs",
      effective_revision: 3,
      updated_at: "2026-07-31T08:00:00Z",
    },
    auth: {
      section: "auth",
      revision: 2,
      schema_version: 1,
      value: { allow_registration: true },
      effect_scope: "new_requests",
      effective_revision: 2,
      updated_at: "2026-07-31T08:00:00Z",
    },
    quotas: {
      section: "quotas",
      revision: 4,
      schema_version: 1,
      value: {
        default_member_limit: 20,
        default_storage_bytes_limit: 5_368_709_120,
        default_concurrent_run_limit: 3,
        default_mcp_calls_daily_limit: 10_000,
        warning_threshold: 0.8,
      },
      effect_scope: "next_authoritative_check",
      effective_revision: 4,
      updated_at: "2026-07-31T08:00:00Z",
    },
  },
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("admin system settings contract", () => {
  test("accepts only the exact public catalog and bounded section values", () => {
    expect(systemSettingsCatalogSchema.parse(catalog)).toEqual(catalog);
    expect(
      systemSettingsCatalogSchema.safeParse({
        ...catalog,
        sections: {
          ...catalog.sections,
          auth: {
            ...catalog.sections.auth,
            value: {
              allow_registration: true,
              client_secret: "must-never-enter-query-cache",
            },
          },
        },
      }).success,
    ).toBe(false);
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeValue,
        title: {
          ...agentRuntimeValue.title,
          prompt_template: "raw prompts are not system-admin fields",
        },
      }).success,
    ).toBe(false);
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeValue,
        title: {
          ...agentRuntimeValue.title,
          model_name: "sk-proj-abcdefgh",
        },
      }).success,
    ).toBe(false);
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeValue,
        tool_output: {
          ...agentRuntimeValue.tool_output,
          storage_subdir: ".secrets",
        },
      }).success,
    ).toBe(false);
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeValue,
        token_budget: {
          ...agentRuntimeValue.token_budget,
          warn_threshold: 0.9,
          hard_stop_threshold: 0.8,
        },
      }).success,
    ).toBe(false);
    expect(
      agentRuntimeSettingsValueSchema.safeParse({
        ...agentRuntimeValue,
        loop_detection: {
          ...agentRuntimeValue.loop_detection,
          warn_threshold: 100_000,
          hard_limit: 100_000,
          window_size: 100_000,
        },
      }).success,
    ).toBe(true);
  });

  test("allows only current active logical model names in editable runtime values", () => {
    expect(
      validateAgentRuntimeModelReferences(agentRuntimeValue, ["analysis-pro"]),
    ).toEqual(agentRuntimeValue);
    expect(() =>
      validateAgentRuntimeModelReferences(agentRuntimeValue, ["vision-active"]),
    ).toThrow();
    expect(
      validateAgentRuntimeModelReferences(
        {
          ...agentRuntimeValue,
          title: { ...agentRuntimeValue.title, model_name: null },
          summarization: {
            ...agentRuntimeValue.summarization,
            model_name: null,
          },
        },
        [],
      ),
    ).toBeDefined();
  });

  test("uses an account-scoped query key", () => {
    expect(adminSystemSettingsQueryKey(ACCOUNT_ID)).toEqual([
      "account",
      ACCOUNT_ID,
      "admin",
      "settings",
      "system",
    ]);
    expect(adminSystemSettingsMutationKey(ACCOUNT_ID)).toEqual([
      "account",
      ACCOUNT_ID,
      "admin",
      "settings",
      "system",
      "mutation",
    ]);
    expect(() => adminSystemSettingsQueryKey("")).toThrow();
  });

  test("loads and replaces one complete section with optimistic concurrency", async () => {
    const controller = new AbortController();
    const mutation = {
      catalog_revision: 8,
      section: "auth",
      stored_revision: 3,
      effective_revision: 3,
      effect_scope: "new_requests",
      effective_at: "2026-07-31T08:01:00Z",
      pending_roles: [],
      policy: {
        revision: 3,
        schema_version: 1,
        value: { allow_registration: false },
      },
    } as const;
    expect(systemSettingsMutationResponseSchema.parse(mutation)).toEqual(
      mutation,
    );
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(200, catalog))
      .mockResolvedValueOnce(jsonResponse(200, mutation));

    await expect(
      fetchAdminSystemSettings(ACCOUNT_ID, controller.signal),
    ).resolves.toEqual(catalog);
    await expect(
      replaceAdminSystemSettingsSection(
        ACCOUNT_ID,
        "auth",
        {
          expected_revision: 2,
          value: { allow_registration: false },
        },
        controller.signal,
      ),
    ).resolves.toEqual(mutation);

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      "/backend/api/admin/settings/system",
      { signal: controller.signal },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      "/backend/api/admin/settings/system/auth",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: 2,
          value: { allow_registration: false },
        }),
        signal: controller.signal,
      },
    );
  });

  test("aborts pending writes on an account transition", async () => {
    let capturedSignal: AbortSignal | undefined;
    const pending = runAbortableAdminSystemSettingsMutation(
      ACCOUNT_ID,
      (signal) =>
        new Promise<never>((_resolve, reject) => {
          capturedSignal = signal;
          signal.addEventListener(
            "abort",
            () =>
              reject(
                Object.assign(new Error("Aborted"), { name: "AbortError" }),
              ),
            { once: true },
          );
        }),
    );

    await Promise.resolve();
    abortAdminSystemSettingsAccount(ACCOUNT_ID);

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(capturedSignal?.aborted).toBe(true);
  });

  test("fails closed when a successful response contains private fields", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, { ...catalog, storage_locator: "private" }),
    );
    await expect(fetchAdminSystemSettings(ACCOUNT_ID)).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
});
