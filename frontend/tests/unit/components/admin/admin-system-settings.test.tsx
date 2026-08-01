import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  usePathname: () => "/admin/settings/system",
  useRouter: () => ({ push: rs.fn() }),
}));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => false }));

import {
  AdminSystemSettingsPage,
  AdminSystemSettingsStateView,
  SystemSettingsEffectBadge,
  type AdminSystemSettingsCatalogState,
} from "@/components/admin/settings/admin-system-settings-page";
import * as adminSystemSettings from "@/core/admin-settings/system";
import { AuthProvider, type User } from "@/core/auth/AuthProvider";
import { I18nProvider } from "@/core/i18n/context";

const runtime = {
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
  input_polish: { enabled: true, max_chars: 4_000, model_name: null },
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
    tool_overrides: {},
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

const catalog = adminSystemSettings.systemSettingsCatalogSchema.parse({
  catalog_revision: 7,
  sections: {
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
    agent_runtime: {
      section: "agent_runtime",
      revision: 3,
      schema_version: 1,
      value: runtime,
      effect_scope: "new_requests_and_runs",
      effective_revision: 3,
      updated_at: "2026-07-31T08:00:00Z",
    },
  },
});

function renderPage(user: User | null): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <QueryClientProvider client={new QueryClient()}>
        <AuthProvider initialUser={user}>
          <AdminSystemSettingsPage />
        </AuthProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

function renderState(
  state: AdminSystemSettingsCatalogState,
  locale: "zh-CN" | "en-US" = "zh-CN",
): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <AdminSystemSettingsStateView
        state={state}
        activeModels={[
          {
            name: "analysis-pro",
            model: "analysis-pro",
            display_name: "Analysis Pro",
            description: "",
            supports_thinking: true,
            supports_reasoning_effort: true,
            supports_vision: false,
            is_default: true,
          },
        ]}
        modelsStatus="ready"
        pendingSection={null}
        sectionErrors={{}}
        lastResults={{
          auth: adminSystemSettings.systemSettingsMutationResponseSchema.parse({
            catalog_revision: 8,
            section: "auth",
            stored_revision: 3,
            effective_revision: 2,
            effect_scope: "new_requests",
            effective_at: "2026-07-31T08:01:00Z",
            pending_roles: ["gateway"],
            policy: {
              revision: 3,
              schema_version: 1,
              value: { allow_registration: false },
            },
          }),
        }}
        onRetry={() => undefined}
        onSave={async () => null}
        retrying={false}
      />
    </I18nProvider>,
  );
}

describe("admin system settings UI", () => {
  test("does not mount a query before a system_admin identity is known", () => {
    const query = rs.spyOn(adminSystemSettings, "useAdminSystemSettings");
    expect(() => renderPage(null)).not.toThrow();
    expect(() =>
      renderPage({
        id: "ordinary-account",
        email: "member@example.com",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      }),
    ).not.toThrow();
    expect(query).not.toHaveBeenCalled();
    query.mockRestore();
  });

  test("renders explicit loading and unavailable states", () => {
    const html = [
      renderState({ status: "loading" }),
      renderState({ status: "error" }),
    ].join("\n");
    expect(html).toContain("正在加载系统配置");
    expect(html).toContain("系统配置暂不可用");
    expect(html).toContain("重试");
    expect(html).toContain('<main id="admin-main"');
  });

  test("renders grouped typed controls, active model choices, and server activation state", () => {
    const html = renderState({ status: "ready", data: catalog });
    for (const value of [
      "账号与访问",
      "默认配额",
      "Agent 运行策略",
      "运行预算",
      "对话体验",
      "上下文与摘要",
      "记忆",
      "工具输出",
      "安全防护",
      "新请求和新任务生效",
      "后续请求生效",
      "下次配额校验生效",
      "已保存为 r3",
      "生效版本 r2",
      "等待进程：gateway",
      "Analysis Pro",
    ]) {
      expect(html).toContain(value);
    }
    expect(html).toContain('name="auth.allow_registration"');
    expect(html).toContain('name="quotas.default_member_limit"');
    expect(html).toContain('name="agent_runtime.title.model_name"');
    expect(html).toContain('<option value="analysis-pro" selected="">');
    expect(html).not.toContain("<textarea");
    expect(html).not.toContain("api_key");
    expect(html).not.toContain("client_secret");
    expect(html).not.toContain("storage_subdir");
    expect(html).not.toContain("prompt_template");
  });

  test("presents system settings as administrator tasks instead of a raw schema editor", () => {
    const html = renderState({ status: "ready", data: catalog });

    for (const value of [
      "选择要管理的配置",
      "注册与访问",
      "项目默认配额",
      "Agent 行为",
      "开放本地账号注册",
      "默认项目存储空间",
      "记录 Token 用量",
      "启用单次 Run Token 预算",
      "总 Token 上限",
      "上下文与摘要",
      "记忆",
      "工具输出",
      "安全防护",
      "未修改",
    ]) {
      expect(html).toContain(value);
    }

    expect(html).toContain('data-settings-navigation="primary"');
    expect(html).not.toContain('role="tablist"');
    expect(html).toContain('data-settings-task="auth"');
    expect(html).toContain('data-settings-task="quotas"');
    expect(html).toContain('data-settings-task="agent_runtime"');
    expect(html).toContain('name="quotas.default_storage_bytes_limit"');
    expect(html).toContain('value="5"');
    expect(html).toContain(">GiB<");
    expect(html).toContain('name="quotas.warning_threshold"');
    expect(html).toContain('value="80"');
    expect(html).toContain(">%<");
    expect(html).toMatch(
      /<input(?=[^>]*name="agent_runtime\.token_budget\.max_tokens")(?=[^>]*data-dependency="token-budget")(?=[^>]*value="200000")(?=[^>]*disabled="")[^>]*>/,
    );
    expect(html).not.toContain("<code");
    expect(html).not.toContain(">agent_runtime.");
  });

  test("uses one aligned workbench with consistent setting rows and footer", () => {
    const html = renderState({ status: "ready", data: catalog });

    expect(html).toContain('data-settings-layout="workbench"');
    expect(html).toContain('data-settings-navigation="primary"');
    expect(html).toContain(
      'data-settings-field-row="agent_runtime.token_budget.max_tokens"',
    );
    expect(html).toContain('data-settings-save-footer="auth"');
    expect(html).not.toContain("data-sticky-save-bar");
  });

  test("localizes effect boundaries without exposing raw enum values", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <div>
          <SystemSettingsEffectBadge scope="new_requests" />
          <SystemSettingsEffectBadge scope="new_runs" />
          <SystemSettingsEffectBadge scope="new_requests_and_runs" />
          <SystemSettingsEffectBadge scope="next_authoritative_check" />
          <SystemSettingsEffectBadge scope="restart_required" />
        </div>
      </I18nProvider>,
    );
    expect(html).toContain("Effective for later requests");
    expect(html).toContain("Applies to new Runs");
    expect(html).toContain("Applies to new requests and Runs");
    expect(html).toContain("Applies at the next quota check");
    expect(html).toContain("Applies after services restart");
    expect(html).not.toContain(">new_requests<");
    expect(html).not.toContain(">restart_required<");
  });
});
