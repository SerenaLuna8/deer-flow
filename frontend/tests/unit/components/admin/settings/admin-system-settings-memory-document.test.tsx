import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AdminSystemSettingsStateView,
  collectDirtySystemSettingsDestinations,
  dirtySystemSettingsDestinations,
  formatSystemDefaultModelOption,
  isSameDocumentNavigation,
  isSystemSettingsSaveDisabled,
  MemoryDocumentSectionsEditor,
  moveMemoryDocumentSection,
  selectVisionInputModels,
} from "@/components/admin/settings/admin-system-settings-page";
import type { SystemSettingsCatalog } from "@/core/admin-settings/system/types";
import { I18nProvider } from "@/core/i18n/context";
import type { Model } from "@/core/models/types";

const TIMESTAMP = "2026-08-09T00:00:00Z";
const OPENAI_VISION_MODEL_ID = "00000000-0000-4000-8000-000000000205";
const ANTHROPIC_VISION_MODEL_ID = "00000000-0000-4000-8000-000000000206";
const TEXT_ONLY_MODEL_ID = "00000000-0000-4000-8000-000000000208";
const MISSING_MODEL_ID = "00000000-0000-4000-8000-000000000207";

function renderChinese(node: React.ReactNode): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

function renderEnglish(node: React.ReactNode): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">{node}</I18nProvider>,
  );
}

function settingsSubsection(html: string, value: string): string {
  const marker = `data-settings-subsection="${value}"`;
  const markerIndex = html.indexOf(marker);
  if (markerIndex < 0) return "";
  const sectionStart = html.lastIndexOf("<section", markerIndex);
  const sectionEnd = html.indexOf("</section>", markerIndex);
  return html.slice(sectionStart, sectionEnd + "</section>".length);
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
        schema_version: 5,
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
            identical_calls: {
              warn_threshold: 3,
              hard_limit: 5,
              window_size: 20,
            },
          },
          internal_tool_call_limit: 200,
          read_before_write: { enabled: true },
          safety_finish_reason: { enabled: true },
          subagents: {
            max_concurrent: 3,
            max_total_per_run_by_workload: {
              interactive: 6,
              research: 9,
            },
          },
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
  test("renders one shared schema v5 internal tool-call limit under Run budget", () => {
    const english = renderEnglish(
      <AdminSystemSettingsStateView
        activeModels={[]}
        lastResults={{}}
        modelsStatus="ready"
        onRetry={rs.fn()}
        onSave={rs.fn(async () => null)}
        pendingSection={null}
        retrying={false}
        sectionErrors={{}}
        state={{ status: "ready", data: catalog() }}
      />,
    );
    const chinese = renderChinese(
      <AdminSystemSettingsStateView
        activeModels={[]}
        lastResults={{}}
        modelsStatus="ready"
        onRetry={rs.fn()}
        onSave={rs.fn(async () => null)}
        pendingSection={null}
        retrying={false}
        sectionErrors={{}}
        state={{ status: "ready", data: catalog() }}
      />,
    );

    expect(english).toContain('data-settings-destination="run-limits"');
    expect(english).toContain('name="agent_runtime.internal_tool_call_limit"');
    expect(english).toContain("Internal tool-call limit per Run");
    expect(english).toContain(
      "Lead Agent and all Sub-Agents share this limit within one Run. Each Run is counted independently, and no new internal tool calls are admitted after the limit is reached.",
    );
    expect(chinese).toContain("每个 Run 内部工具调用上限");
    expect(chinese).toContain(
      "同一 Run 内 Lead Agent 与所有子 Agent 共享此上限；不同 Run 独立计数，达到上限后不再准入新的内部工具调用。",
    );
    expect(english).not.toContain(
      'data-settings-destination="tool-call-budget"',
    );
    expect(english).not.toContain("agent_runtime.tool_call_budget");
    expect(english).toContain(
      'name="agent_runtime.loop_detection.identical_calls.window_size"',
    );
    expect(english).toContain(
      'name="agent_runtime.subagents.max_total_per_run_by_workload.research"',
    );
    expect(english).not.toContain("tool_freq_hard_limit");

    const executionLimits = settingsSubsection(english, "run-execution");
    const tokenBudget = settingsSubsection(english, "token-budget");
    expect(executionLimits).toContain(
      'name="agent_runtime.max_recursion_limit"',
    );
    expect(executionLimits).toContain(
      'name="agent_runtime.internal_tool_call_limit"',
    );
    expect(executionLimits).not.toContain("agent_runtime.token_budget");
    expect(tokenBudget).toContain('name="agent_runtime.token_budget.enabled"');
    expect(tokenBudget).toContain(
      'name="agent_runtime.token_budget.max_tokens"',
    );
    expect(tokenBudget).not.toContain("agent_runtime.internal_tool_call_limit");
    expect(
      english.indexOf('data-settings-subsection="run-execution"'),
    ).toBeLessThan(english.indexOf('data-settings-subsection="token-budget"'));
    expect(chinese).toContain("始终生效，不受 Token 预算开关影响。");
    expect(chinese).toContain("只有本区块中的上限与阈值受开关控制。");
  });

  test("treats a same-page hash target as navigation within the current document", () => {
    expect(
      isSameDocumentNavigation(
        "https://actweave.test/admin/settings/system",
        "https://actweave.test/admin/settings/system#admin-main",
      ),
    ).toBe(true);
    expect(
      isSameDocumentNavigation(
        "https://actweave.test/admin/settings/system?group=memory",
        "https://actweave.test/admin/settings/system?group=runtime#admin-main",
      ),
    ).toBe(false);
    expect(
      isSameDocumentNavigation(
        "https://actweave.test/admin/settings/system",
        "https://actweave.test/admin/settings/models",
      ),
    ).toBe(false);
  });

  test("identifies the exact navigation destinations with retained drafts", () => {
    const data = catalog();
    const runtimeBase = data.sections.agent_runtime.value;
    const runtimeDraft = structuredClone(runtimeBase);
    runtimeDraft.internal_tool_call_limit += 1;
    runtimeDraft.title.max_chars += 1;

    expect(
      dirtySystemSettingsDestinations(
        "agent_runtime",
        runtimeBase,
        runtimeDraft,
      ),
    ).toEqual(["run-limits", "assistant-experience"]);
    expect(
      dirtySystemSettingsDestinations(
        "memory_document",
        data.sections.memory_document.value,
        { sections: ["用户偏好", "项目背景", "长期目标"] },
      ),
    ).toEqual(["memory"]);
    expect(
      collectDirtySystemSettingsDestinations({
        agent_runtime: ["run-limits", "assistant-experience"],
        memory_document: ["memory"],
      }),
    ).toEqual(new Set(["run-limits", "assistant-experience", "memory"]));
  });

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

  test("localizes Memory minute units in English", () => {
    const html = renderEnglish(
      <AdminSystemSettingsStateView
        activeModels={[]}
        lastResults={{}}
        modelsStatus="ready"
        onRetry={rs.fn()}
        onSave={rs.fn(async () => null)}
        pendingSection={null}
        retrying={false}
        sectionErrors={{}}
        state={{ status: "ready", data: catalog() }}
      />,
    );

    expect(html).toContain("minutes");
    expect(html).not.toContain("分钟");
  });

  test("offers every vision-capable model and ignores the legacy Bridge flag", () => {
    const data = catalog();
    data.sections.agent_runtime.value.vision_bridge.model_name =
      OPENAI_VISION_MODEL_ID;
    const activeModels: Model[] = [
      {
        name: OPENAI_VISION_MODEL_ID,
        model: OPENAI_VISION_MODEL_ID,
        display_name: "OpenAI vision model",
        supports_thinking: false,
        supports_reasoning_effort: false,
        supports_vision: true,
        supports_vision_bridge: false,
        is_default: false,
      },
      {
        name: ANTHROPIC_VISION_MODEL_ID,
        model: ANTHROPIC_VISION_MODEL_ID,
        display_name: "Anthropic vision model",
        supports_thinking: false,
        supports_reasoning_effort: false,
        supports_vision: true,
        supports_vision_bridge: true,
        is_default: false,
      },
      {
        name: TEXT_ONLY_MODEL_ID,
        model: TEXT_ONLY_MODEL_ID,
        display_name: "Legacy-qualified text-only model",
        supports_thinking: false,
        supports_reasoning_effort: false,
        supports_vision: false,
        supports_vision_bridge: true,
        is_default: false,
      },
    ];

    expect(
      selectVisionInputModels(activeModels).map((model) => model.name),
    ).toEqual([OPENAI_VISION_MODEL_ID, ANTHROPIC_VISION_MODEL_ID]);

    const html = renderChinese(
      <AdminSystemSettingsStateView
        activeModels={activeModels}
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
    const visionModelSelect =
      /<select name="agent_runtime\.vision_bridge\.model_name"[\s\S]*?<\/select>/u.exec(
        html,
      )?.[0] ?? "";
    expect(visionModelSelect).toContain(
      'name="agent_runtime.vision_bridge.model_name"',
    );
    expect(visionModelSelect).toContain(
      `value="${OPENAI_VISION_MODEL_ID}" selected=""`,
    );
    expect(visionModelSelect).toContain("OpenAI vision model");
    expect(visionModelSelect).toContain("Anthropic vision model");
    expect(visionModelSelect).not.toContain("Legacy-qualified text-only model");
    expect(html).toContain("视觉模型");
    expect(html).toContain("支持视觉输入");
    expect(html).not.toContain("图片识别桥接");
    expect(visionModelSelect).toContain(">关闭</option>");
    expect(html).not.toContain("egress_grant");
    expect(html).not.toContain("agent_runtime.vision_bridge.enabled");
  });

  test("does not display an unavailable internal model reference", () => {
    const data = catalog();
    data.sections.agent_runtime.value.vision_bridge.model_name =
      MISSING_MODEL_ID;
    const html = renderChinese(
      <AdminSystemSettingsStateView
        activeModels={[]}
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

    expect(html).toContain("当前引用的模型已不可用");
    expect(html).not.toContain(`>${MISSING_MODEL_ID}`);
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
