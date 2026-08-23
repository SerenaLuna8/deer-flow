import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ContextWindowDetails,
  ContextWindowIndicator,
} from "@/components/workspace/context-window-indicator";
import { I18nProvider } from "@/core/i18n/context";
import type {
  ContextUsageTrigger,
  ThreadContextUsageResponse,
} from "@/core/threads/context-usage";

const THREAD_ID = "33333333-3333-4333-8333-333333333333";

function usage(
  primary: ContextUsageTrigger | null,
  overrides: Partial<ThreadContextUsageResponse> = {},
): ThreadContextUsageResponse {
  return {
    thread_id: THREAD_ID,
    enabled: true,
    estimated_tokens: 16_000,
    error_allowance_tokens: 3_200,
    safety_bound_tokens: 19_200,
    provider_input_tokens: null,
    estimator_revision: "provider-request-engineering-v1",
    error_contract:
      "versioned_engineering_allowance_for_app_owned_serialized_material_plus_declared_provider_overhead",
    components: {
      compressible: {
        estimated_tokens: 12_000,
        error_allowance_tokens: 2_400,
        safety_bound_tokens: 14_400,
      },
      fixed: {
        estimated_tokens: 3_000,
        error_allowance_tokens: 600,
        safety_bound_tokens: 3_600,
      },
      ephemeral: {
        estimated_tokens: 1_000,
        error_allowance_tokens: 200,
        safety_bound_tokens: 1_200,
      },
    },
    fixed_over_trigger: false,
    message_count: 12,
    summary_present: false,
    context_window_tokens: 128_000,
    triggers: primary ? [primary] : [],
    primary_trigger: primary,
    ...overrides,
  };
}

function render(node: React.ReactNode, locale: "zh-CN" | "en-US" = "zh-CN") {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>{node}</I18nProvider>,
  );
}

describe("ContextWindowIndicator", () => {
  test("renders only current context, compression trigger, total context, and one occupancy bar", () => {
    const value = usage(
      {
        type: "tokens",
        configured_value: 32_000,
        current_value: 16_000,
        threshold_value: 32_000,
        remaining_value: 16_000,
        progress_percent: 50,
        reached: false,
        threshold_tokens: 32_000,
      },
      { provider_input_tokens: 13_500 },
    );

    const details = render(<ContextWindowDetails usage={value} />);
    const indicator = render(<ContextWindowIndicator usage={value} />);

    expect(details.match(/data-slot="progress"/g)).toHaveLength(1);
    expect(details).toContain('data-context-progress-state="ready"');
    expect(details).toContain('aria-valuenow="12.5"');
    expect(details).toContain("当前上下文");
    expect(details).toContain("压缩触发");
    expect(details).toContain("总上下文");
    expect(details).toContain("16.0K Tokens");
    expect(details).toContain("32.0K Tokens");
    expect(details).toContain("128.0K Tokens");
    expect(details).not.toContain("安全占用上界");
    expect(details).not.toContain("上次供应商实测");
    expect(details).not.toContain("自动压缩条件");
    expect(details).not.toContain("Token 触发条件");
    expect(details).not.toContain("当前条件值");
    expect(details).not.toContain("已达到触发条件");
    expect(details).not.toContain("13.5K Tokens");
    expect(details).not.toContain("50%");
    expect(indicator).toContain('data-context-window-state="ready"');
    expect(indicator).toContain('data-progress="12.5"');
    expect(indicator).toContain("估算上下文占用 12.5%");
  });

  test("converts a fraction trigger to its Token threshold", () => {
    const value = usage(
      {
        type: "fraction",
        configured_value: 0.8,
        current_value: 0.45,
        threshold_value: 0.8,
        remaining_value: 0.35,
        progress_percent: 56.25,
        reached: false,
        context_window_tokens: 258_000,
        threshold_tokens: 206_400,
      },
      {
        estimated_tokens: 115_000,
        error_allowance_tokens: 23_000,
        safety_bound_tokens: 138_000,
        context_window_tokens: 258_000,
        summary_present: true,
      },
    );

    const details = render(<ContextWindowDetails usage={value} />);

    expect(details).toContain('aria-valuenow="44.57"');
    expect(details).toContain("当前上下文");
    expect(details).toContain("115.0K Tokens");
    expect(details).toContain("压缩触发");
    expect(details).toContain("206.4K Tokens");
    expect(details).toContain("总上下文");
    expect(details).toContain("258.0K Tokens");
    expect(details).not.toContain("百分比触发条件");
    expect(details).not.toContain("45%");
    expect(details).not.toContain("80%");
    expect(details).not.toContain("138.0K Tokens");
    expect(details).not.toContain("已包含上次压缩摘要");
  });

  test("keeps one unavailable occupancy bar when the total context is unknown", () => {
    const value = usage(
      {
        type: "tokens",
        configured_value: 32_000,
        current_value: 115_344,
        threshold_value: 32_000,
        remaining_value: 0,
        progress_percent: 100,
        reached: true,
        threshold_tokens: 32_000,
      },
      {
        estimated_tokens: 92_652,
        error_allowance_tokens: 22_692,
        safety_bound_tokens: 115_344,
        context_window_tokens: null,
      },
    );

    const details = render(<ContextWindowDetails usage={value} />);
    const indicator = render(<ContextWindowIndicator usage={value} />);

    expect(details.match(/data-slot="progress"/g)).toHaveLength(1);
    expect(details).toContain('data-context-progress-state="unavailable"');
    expect(details).toContain('aria-disabled="true"');
    expect(details).not.toContain("aria-valuenow");
    expect(details).toContain("当前上下文");
    expect(details).toContain("92.7K Tokens");
    expect(details).toContain("压缩触发");
    expect(details).toContain("32.0K Tokens");
    expect(details).toContain("总上下文");
    expect(details).toContain("未配置");
    expect(details).not.toContain("安全占用上界");
    expect(details).not.toContain("115.3K Tokens");
    expect(details).not.toContain("已达到触发条件");
    expect(indicator).toContain('data-context-window-state="ready"');
    expect(indicator).not.toContain("data-progress");
    expect(indicator).toContain("估算上下文 92.7K Tokens，窗口上限未配置");
  });

  test("shows the real message threshold when compression is message-based", () => {
    const value = usage({
      type: "messages",
      configured_value: 20,
      current_value: 12,
      threshold_value: 20,
      remaining_value: 8,
      progress_percent: 60,
      reached: false,
    });

    const details = render(<ContextWindowDetails usage={value} />);

    expect(details).toContain("压缩触发");
    expect(details).toContain("20 条消息");
    expect(details).not.toContain("消息数触发条件");
    expect(details).not.toContain("12 条消息");
    expect(details).not.toContain("8 条消息");
  });

  test("shows only the server-selected compression threshold for OR triggers", () => {
    const tokens: ContextUsageTrigger = {
      type: "tokens",
      configured_value: 32_000,
      current_value: 16_000,
      threshold_value: 32_000,
      remaining_value: 16_000,
      progress_percent: 50,
      reached: false,
      threshold_tokens: 32_000,
    };
    const fraction: ContextUsageTrigger = {
      type: "fraction",
      configured_value: 0.8,
      current_value: 0.6,
      threshold_value: 0.8,
      remaining_value: 0.2,
      progress_percent: 75,
      reached: false,
      context_window_tokens: 128_000,
      threshold_tokens: 102_400,
    };
    const value = usage(fraction, { triggers: [tokens, fraction] });

    const details = render(<ContextWindowDetails usage={value} />);

    expect(details).toContain("压缩触发");
    expect(details).toContain("102.4K Tokens");
    expect(details).not.toContain("32.0K Tokens");
    expect(details).not.toContain("多个条件任一达到即自动压缩");
    expect(details).not.toContain("Token 触发条件");
    expect(details).not.toContain("百分比触发条件");
  });

  test("keeps context usage available when automatic compression is disabled", () => {
    const disabled = usage(null, { enabled: false });

    const details = render(<ContextWindowDetails usage={disabled} />);
    const indicator = render(<ContextWindowIndicator usage={disabled} />);

    expect(details).toContain("当前上下文");
    expect(details).toContain("16.0K Tokens");
    expect(details).toContain("压缩触发");
    expect(details).toContain("已关闭");
    expect(details).toContain("总上下文");
    expect(details).toContain("128.0K Tokens");
    expect(details).not.toContain("自动压缩条件");
    expect(indicator).toContain('data-context-window-state="ready"');
    expect(indicator).toContain('data-progress="12.5"');
  });

  test("shows an unconfigured compression trigger without adding another section", () => {
    const value = usage(null);

    const details = render(<ContextWindowDetails usage={value} />);

    expect(details).toContain("压缩触发");
    expect(details).toContain("未配置");
    expect(details).not.toContain("自动压缩条件");
  });

  test("keeps loading and unavailable states explicit", () => {
    expect(render(<ContextWindowIndicator isLoading />)).toContain(
      'data-context-window-state="loading"',
    );
    expect(
      render(<ContextWindowIndicator error={new Error("offline")} />),
    ).toContain('data-context-window-state="unavailable"');
  });

  test("caps context occupancy at 100 percent", () => {
    const value = usage(null, {
      estimated_tokens: 130_000,
      safety_bound_tokens: 156_000,
      context_window_tokens: 128_000,
    });

    const indicator = render(<ContextWindowIndicator usage={value} />);

    expect(indicator).toContain('data-progress="100"');
    expect(indicator).toContain("估算上下文占用 100%");
  });
});
