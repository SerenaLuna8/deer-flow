import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AdminSystemSettingsStateView,
  formatSystemDefaultModelOption,
  isSystemSettingsSaveDisabled,
  MemoryDocumentSectionsEditor,
  moveMemoryDocumentSection,
} from "@/components/admin/settings/admin-system-settings-page";
import type { SystemSettingsCatalog } from "@/core/admin-settings/system/types";
import { I18nProvider } from "@/core/i18n/context";

const TIMESTAMP = "2026-08-09T00:00:00Z";

function renderChinese(node: React.ReactNode): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

function catalog(): SystemSettingsCatalog {
  return {
    catalog_revision: 9,
    sections: {
      auth: {
        section: "auth",
        revision: 2,
        schema_version: 2,
        effective_revision: 2,
        effect_scope: "new_requests",
        updated_at: TIMESTAMP,
        value: { allow_registration: true },
      },
      automations: {
        section: "automations",
        revision: 1,
        schema_version: 2,
        effective_revision: 1,
        effect_scope: "new_requests",
        updated_at: TIMESTAMP,
        value: {
          enabled: true,
          poll_interval_seconds: 5,
          max_concurrent_runs: 3,
          min_once_delay_seconds: 60,
        },
      },
      quotas: {
        section: "quotas",
        revision: 4,
        schema_version: 2,
        effective_revision: 4,
        effect_scope: "next_authoritative_check",
        updated_at: TIMESTAMP,
        value: {
          default_member_limit: 10,
          default_storage_bytes_limit: 1_073_741_824,
          default_concurrent_run_limit: 4,
          default_mcp_calls_daily_limit: 1_000,
          warning_threshold: 0.8,
        },
      },
      agent_runtime: {
        section: "agent_runtime",
        revision: 6,
        schema_version: 2,
        effective_revision: 6,
        effect_scope: "new_requests_and_runs",
        updated_at: TIMESTAMP,
        value: {
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
          input_polish: {
            enabled: true,
            max_chars: 10_000,
            model_name: null,
          },
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
        },
      },
      memory_document: {
        section: "memory_document",
        revision: 7,
        schema_version: 2,
        effective_revision: 7,
        effect_scope: "new_memory_documents",
        updated_at: TIMESTAMP,
        value: { sections: ["用户偏好", "项目背景"] },
      },
    },
  } as SystemSettingsCatalog;
}

describe("admin Memory document settings", () => {
  test("renders accessible ordered controls and moves titles without mutating the source", () => {
    const value = { sections: ["用户偏好", "项目背景", "长期目标"] };
    const moved = moveMemoryDocumentSection(value, 1, -1);

    expect(moved.sections).toEqual(["项目背景", "用户偏好", "长期目标"]);
    expect(value.sections).toEqual(["用户偏好", "项目背景", "长期目标"]);
    expect(moveMemoryDocumentSection(value, 0, -1)).toBe(value);

    const html = renderChinese(
      <MemoryDocumentSectionsEditor value={value} onChange={rs.fn()} />,
    );
    expect(html).toContain('name="memory_document.sections.0"');
    expect(html).toContain('aria-label="上移章节 1"');
    expect(html).toContain('aria-label="下移章节 1"');
    expect(html).toContain('aria-label="删除章节 1"');
    expect(html).toContain("不要输入 # 或 Dream 历史标记");
  });

  test("keeps the document save gate independent from model catalog state", () => {
    expect(
      isSystemSettingsSaveDisabled({
        dirty: true,
        modelsStatus: "error",
        pending: false,
        schemaValid: true,
        section: "memory_document",
      }),
    ).toBe(false);
    expect(
      isSystemSettingsSaveDisabled({
        dirty: true,
        modelsStatus: "ready",
        pending: false,
        schemaValid: false,
        section: "memory_document",
      }),
    ).toBe(true);
    expect(
      isSystemSettingsSaveDisabled({
        dirty: true,
        modelsStatus: "error",
        pending: false,
        schemaValid: true,
        section: "agent_runtime",
      }),
    ).toBe(true);
  });

  test("keeps one Memory destination with separate runtime and document forms", () => {
    const html = renderChinese(
      <AdminSystemSettingsStateView
        activeModels={[]}
        lastResults={{}}
        modelsStatus="error"
        onRetry={rs.fn()}
        onSave={rs.fn(async () => null)}
        pendingSection={null}
        retrying={false}
        sectionErrors={{}}
        state={{ status: "ready", data: catalog() }}
      />,
    );

    expect(html.match(/data-settings-destination="memory"/gu)).toHaveLength(1);
    expect(html).toContain('data-settings-destination="automations"');
    expect(html).toContain("bg-blue-50 text-blue-600");
    expect(html).toContain("hover:bg-blue-50");
    expect(html).toContain('data-settings-save-footer="agent_runtime"');
    expect(html).toContain('data-settings-save-footer="memory_document"');
    expect(html).toContain(
      "仅影响新建文档；已有文档继续使用创建时冻结的章节结构",
    );
  });

  test("renders Vision Bridge as model selection without an enable or grant switch", () => {
    const data = catalog();
    data.sections.agent_runtime.value.vision_bridge.model_name =
      "small-vision-model";
    const html = renderChinese(
      <AdminSystemSettingsStateView
        activeModels={[
          {
            name: "small-vision-model",
            model: "small-vision-model",
            display_name: "Small Vision Model",
            description: "",
            supports_thinking: false,
            supports_reasoning_effort: false,
            supports_vision: true,
            supports_vision_bridge: true,
            is_default: false,
          },
        ]}
        lastResults={{}}
        modelsStatus="ready"
        onRetry={rs.fn()}
        onSave={rs.fn(async () => null)}
        pendingSection={null}
        retrying={false}
        sectionErrors={{}}
        state={{ status: "ready", data }}
      />,
    );

    expect(html).toContain('data-settings-destination="vision-bridge"');
    expect(html).toContain('name="agent_runtime.vision_bridge.model_name"');
    expect(html).toContain('value="small-vision-model" selected=""');
    expect(html).toContain(">关闭</option>");
    expect(html).not.toContain("egress_grant");
    expect(html).not.toContain("agent_runtime.vision_bridge.enabled");
  });
});

describe("formatSystemDefaultModelOption", () => {
  test("keeps the fallback label when no default model is published", () => {
    expect(formatSystemDefaultModelOption("使用系统默认模型", undefined)).toBe(
      "使用系统默认模型",
    );
  });

  test("names the current default model in the empty option", () => {
    expect(
      formatSystemDefaultModelOption(
        "使用系统默认模型",
        "DeepSeek Flash",
        "zh-CN",
      ),
    ).toBe("使用系统默认模型（DeepSeek Flash）");
    expect(
      formatSystemDefaultModelOption(
        "Use the system default model",
        "DeepSeek Flash",
        "en-US",
      ),
    ).toBe("Use the system default model (DeepSeek Flash)");
  });
});
