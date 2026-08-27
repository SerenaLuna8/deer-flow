import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ContextWindowDetails,
  ContextWindowIndicator,
} from "@/components/workspace/context-window-indicator";
import { I18nProvider } from "@/core/i18n/context";
import type { ThreadContextProjection } from "@/core/threads/context-usage";

const THREAD_ID = "33333333-3333-4333-8333-333333333333";

function usage(
  overrides: Partial<ThreadContextProjection> = {},
): ThreadContextProjection {
  return {
    contract_version: 2,
    thread_id: THREAD_ID,
    subject: {
      kind: "lead_thread",
      thread_id: THREAD_ID,
      execution_id: null,
    },
    phase: "idle",
    projection_seq: "12",
    evidence_seq: "11",
    context_window_generation: "55555555-5555-4555-8555-555555555555",
    checkpoint_id: "checkpoint-12",
    projector_revision: "context-projector-v2",
    model: {
      identity_digest:
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      context_window_tokens: 300_000,
    },
    basis: "hybrid",
    coverage: "complete",
    freshness: "current",
    totals: {
      projected_tokens: 134_100,
      lower_bound_tokens: 134_100,
      safety_upper_bound_tokens: 141_000,
      context_window_tokens: 300_000,
      remaining_tokens: 165_900,
      progress_percent: 44.7,
    },
    lanes: [
      lane("system_prompt", 5_700),
      lane("agent_instructions", 4_800),
      lane("tool_definitions", 23_500),
      lane("skills", 2_800),
      lane("mcp_dynamic_tools", 3_300),
      lane("subagent_definitions", 1_600),
      lane("summarized_conversation", 7_400),
      lane("conversation", 85_000),
      lane("visual_media", 0),
      lane("provider_overhead", 0),
    ],
    last_provider_observation: {
      provider_call_id: "b".repeat(64),
      input_tokens: 132_800,
      observed_at: "2026-08-27T08:00:00Z",
    },
    compaction: {
      enabled: true,
      threshold_tokens: 240_000,
      reached: false,
      authority: "idle_history",
      blocked_reason: null,
    },
    notices: [],
    as_of: "2026-08-27T08:00:01Z",
    ...overrides,
  };
}

function lane(
  name: ThreadContextProjection["lanes"][number]["lane"],
  tokens: number,
): ThreadContextProjection["lanes"][number] {
  return {
    lane: name,
    projected_tokens: tokens,
    lower_bound_tokens: tokens,
    safety_upper_bound_tokens: tokens,
  };
}

function render(node: React.ReactNode, locale: "zh-CN" | "en-US" = "zh-CN") {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>{node}</I18nProvider>,
  );
}

describe("Context Usage v2 presentation", () => {
  test("renders the image-style total, segmented occupancy, fixed lane order, and separate Provider observation", () => {
    const details = render(<ContextWindowDetails usage={usage()} />);
    const indicator = render(<ContextWindowIndicator usage={usage()} />);

    expect(details).toContain('data-context-total-bound="approximate"');
    expect(details).toContain("~134.1K / 300.0K Tokens");
    expect(details).toContain("45% 已使用");
    expect(details).toContain('role="progressbar"');
    expect(details).toContain('aria-valuenow="44.7"');
    expect(details).toContain("上次 Provider 输入");
    expect(details).toContain("132.8K");
    expect(details).toContain("安全占用上界");
    expect(details).toContain("141.0K");
    expect(details).toContain("自动压缩线");
    expect(details).toContain("240.0K");

    const labels = [
      "系统提示词",
      "Agent 指令",
      "工具定义",
      "Skills",
      "MCP 与动态工具",
      "子 Agent 定义",
      "压缩摘要",
      "对话历史",
    ];
    labels.forEach((label) => expect(details).toContain(label));
    labels.slice(1).forEach((label, index) => {
      expect(details.indexOf(labels[index]!)).toBeLessThan(
        details.indexOf(label),
      );
    });
    expect(details).not.toContain("图片与媒体");
    expect(details).not.toContain("Provider 请求开销");
    expect(indicator).toContain('data-context-window-state="ready"');
    expect(indicator).toContain('data-progress="44.7"');
  });

  test("uses a lower bound and keeps stale partial projections visible", () => {
    const complete = usage();
    const value = usage({
      coverage: "partial",
      freshness: "stale",
      totals: {
        ...complete.totals,
        safety_upper_bound_tokens: null,
      },
      lanes: complete.lanes.map((laneValue) =>
        laneValue.lane === "visual_media"
          ? { ...laneValue, safety_upper_bound_tokens: null }
          : laneValue,
      ),
      notices: [
        { code: "VISUAL_COST_UNMEASURED", count: 2, lane: "visual_media" },
        { code: "PROJECTION_STALE", count: null, lane: null },
      ],
    });

    const details = render(<ContextWindowDetails usage={value} />);
    const indicator = render(<ContextWindowIndicator usage={value} />);

    expect(details).toContain('data-context-total-bound="lower"');
    expect(details).toContain("≥134.1K / 300.0K Tokens · 数据已过期");
    expect(details).toContain("另有 2 张图片尚未计量");
    expect(details).toContain("数据已过期");
    expect(indicator).toContain('data-context-window-state="ready"');
    expect(indicator).toContain("上下文至少 134.1K");
  });

  test("shows absolute Token information and no percentage when capacity is unknown", () => {
    const value = usage({
      model: {
        identity_digest:
          "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        context_window_tokens: null,
      },
      totals: {
        projected_tokens: 134_100,
        lower_bound_tokens: 134_100,
        safety_upper_bound_tokens: 141_000,
        context_window_tokens: null,
        remaining_tokens: null,
        progress_percent: null,
      },
      notices: [{ code: "CAPACITY_UNKNOWN", count: null, lane: null }],
    });

    const details = render(<ContextWindowDetails usage={value} />);
    const indicator = render(<ContextWindowIndicator usage={value} />);

    expect(details).toContain("~134.1K Tokens");
    expect(details).not.toContain("% 已使用");
    expect(details).not.toContain('role="progressbar"');
    expect(details).toContain("模型上下文容量未知");
    expect(indicator).not.toContain("data-progress");
    expect(indicator).toContain("上下文约 134.1K，窗口容量未知");
  });

  test("keeps loading and true unavailability explicit", () => {
    expect(render(<ContextWindowIndicator isLoading />)).toContain(
      'data-context-window-state="loading"',
    );
    expect(
      render(<ContextWindowIndicator error={new Error("offline")} />),
    ).toContain('data-context-window-state="unavailable"');
  });
});
