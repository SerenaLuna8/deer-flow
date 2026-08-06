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
  test("renders Token progress against the configured compression threshold", () => {
    const value = usage({
      type: "tokens",
      configured_value: 32_000,
      current_value: 16_000,
      threshold_value: 32_000,
      remaining_value: 16_000,
      progress_percent: 50,
      reached: false,
      threshold_tokens: 32_000,
    });

    const details = render(<ContextWindowDetails usage={value} />);
    const indicator = render(<ContextWindowIndicator usage={value} />);

    expect(details).toContain("Token 触发条件");
    expect(details).toContain("16.0K Tokens");
    expect(details).toContain("32.0K Tokens");
    expect(details).toContain("剩余");
    expect(indicator).toContain('data-context-window-state="ready"');
    expect(indicator).toContain('data-progress="50"');
    expect(indicator).toContain("已达到自动压缩阈值的 50%");
  });

  test("renders percentage progress as window occupancy and keeps Token estimates secondary", () => {
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
        context_window_tokens: 258_000,
        summary_present: true,
      },
    );

    const details = render(<ContextWindowDetails usage={value} />);

    expect(details).toContain("百分比触发条件");
    expect(details).toContain("45%");
    expect(details).toContain("80%");
    expect(details).toContain("35%");
    expect(details).toContain("115.0K / 258.0K Tokens");
    expect(details).toContain("已包含上次压缩摘要");
  });

  test("renders message progress in messages instead of pretending it is Token usage", () => {
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

    expect(details).toContain("消息数触发条件");
    expect(details).toContain("12 条消息");
    expect(details).toContain("20 条消息");
    expect(details).toContain("8 条消息");
  });

  test("lists every OR trigger while keeping the server-selected primary trigger", () => {
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

    expect(details).toContain("多个条件任一达到即自动压缩");
    expect(details).toContain("Token 触发条件");
    expect(details).toContain("百分比触发条件");
    expect(details.indexOf("百分比触发条件")).toBeLessThan(
      details.lastIndexOf("Token 触发条件"),
    );
  });

  test("keeps loading, unavailable, and disabled states explicit", () => {
    const disabled = usage(null, { enabled: false });

    expect(render(<ContextWindowIndicator isLoading />)).toContain(
      'data-context-window-state="loading"',
    );
    expect(
      render(<ContextWindowIndicator error={new Error("offline")} />),
    ).toContain('data-context-window-state="unavailable"');
    expect(render(<ContextWindowIndicator usage={disabled} />)).toContain(
      'data-context-window-state="disabled"',
    );
  });
});
