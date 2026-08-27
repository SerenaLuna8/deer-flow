import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { RunControlProgress } from "@/components/workspace/run-control-progress";
import { I18nProvider } from "@/core/i18n/context";
import {
  parseRepeatedCallEvent,
  parseSubagentLimitEvent,
  parseToolCallBudgetEvent,
  type RunControlObservation,
} from "@/core/threads/tool-call-control-events";

const RUN_ID = "50000000-0000-4000-8000-000000000001";

function observation(
  reasonCode:
    | "repeated_call_warning"
    | "repeated_call_limit"
    | "tool_budget_warning"
    | "tool_budget_exhausted"
    | "subagent_total_limit",
  digestCharacter: string,
): RunControlObservation {
  if (reasonCode === "subagent_total_limit") {
    const event = parseSubagentLimitEvent({
      type: "subagent_limit",
      schema_version: 1,
      reason_code: "subagent_total_limit",
      role: "lead",
      run_id: RUN_ID,
      count_before: 5,
      proposed: 1,
      admitted: 0,
      rejected: 1,
      count_after: 5,
      hard_limit: 5,
      disposition: "truncate_tool_calls",
      observation_id: digestCharacter.repeat(64),
    });
    if (!event) throw new Error(`fixture rejected: ${reasonCode}`);
    return event;
  }
  const repeated = reasonCode.startsWith("repeated_call");
  const repeatedLimit = reasonCode === "repeated_call_limit";
  const toolBudgetExhausted = reasonCode === "tool_budget_exhausted";
  const common = {
    schema_version: 1,
    reason_code: reasonCode,
    workload_profile: "research",
    role: "lead",
    run_id: RUN_ID,
    execution_id: null,
    count_before: repeatedLimit || toolBudgetExhausted ? 4 : 2,
    proposed: 1,
    admitted: repeatedLimit ? 0 : 1,
    rejected: repeatedLimit ? 1 : 0,
    count_after: repeatedLimit || toolBudgetExhausted ? 5 : 3,
    warn_threshold: 3,
    hard_limit: 5,
    disposition: repeatedLimit
      ? "tool_free_finalization"
      : toolBudgetExhausted
        ? "exhaust_tool"
        : "advisory",
    observation_id: digestCharacter.repeat(64),
  } as const;
  if (!repeated && toolBudgetExhausted) {
    const event = parseToolCallBudgetEvent({
      type: "tool_call_budget",
      schema_version: 2,
      reason_code: "tool_budget_exhausted",
      workload_profile: "research",
      role: "lead",
      run_id: RUN_ID,
      execution_id: null,
      count_before: 4,
      proposed: 1,
      admitted: 1,
      rejected: 0,
      count_after: 5,
      hard_limit: 5,
      disposition: "exhaust_run",
      observation_id: digestCharacter.repeat(64),
    });
    if (!event) throw new Error(`fixture rejected: ${reasonCode}`);
    return event;
  }
  const event = repeated
    ? parseRepeatedCallEvent({ type: "repeated_call", ...common })
    : parseToolCallBudgetEvent({
        type: "tool_call_budget",
        ...common,
        tool_name: "web_search",
      });
  if (!event) throw new Error(`fixture rejected: ${reasonCode}`);
  return event;
}

function render(
  observations: RunControlObservation[],
  locale: "zh-CN" | "en-US",
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <RunControlProgress observations={observations} />
    </I18nProvider>,
  );
}

function subjectBudgetObservation(
  budgetScope: "lead" | "subagent_task",
  digestCharacter: string,
): RunControlObservation {
  const subagentTask = budgetScope === "subagent_task";
  const hardLimit = subagentTask ? 50 : 200;
  const event = parseToolCallBudgetEvent({
    type: "tool_call_budget",
    schema_version: 3,
    budget_scope: budgetScope,
    reason_code: "tool_budget_exhausted",
    workload_profile: "research",
    role: subagentTask ? "subagent" : "lead",
    run_id: RUN_ID,
    execution_id: subagentTask ? "d".repeat(32) : null,
    count_before: hardLimit - 1,
    proposed: 1,
    admitted: 1,
    rejected: 0,
    count_after: hardLimit,
    hard_limit: hardLimit,
    disposition: "exhaust_subject",
    observation_id: digestCharacter.repeat(64),
  });
  if (!event)
    throw new Error(`subject budget fixture rejected: ${budgetScope}`);
  return event;
}

describe("RunControlProgress", () => {
  test("hides repeated-call advisories but keeps the hard limit visible", () => {
    const mixed = render(
      [
        observation("repeated_call_warning", "a"),
        observation("repeated_call_limit", "b"),
      ],
      "en-US",
    );
    const warningOnly = render(
      [observation("repeated_call_warning", "c")],
      "zh-CN",
    );

    expect(mixed).not.toContain("Repeated tool-call pattern detected");
    expect(mixed).toContain("Repeated-call limit reached");
    expect(mixed).not.toContain('data-reason-code="repeated_call_warning"');
    expect(mixed).toContain('data-reason-code="repeated_call_limit"');
    expect(warningOnly).toBe("");
  });

  test("hides tool-budget warnings but keeps exhaustion visible and non-terminal", () => {
    const english = render(
      [
        observation("tool_budget_warning", "c"),
        observation("tool_budget_exhausted", "d"),
      ],
      "en-US",
    );
    const chinese = render(
      [observation("tool_budget_exhausted", "e")],
      "zh-CN",
    );
    const warningOnly = render(
      [observation("tool_budget_warning", "f")],
      "zh-CN",
    );

    expect(english).not.toContain("web_search is nearing its call limit");
    expect(english).not.toContain('data-reason-code="tool_budget_warning"');
    expect(english).toContain("Run internal tool-call limit reached");
    expect(english).toContain(
      "No new internal tool calls can be admitted in this Run",
    );
    expect(english).not.toContain("Run did not finish");
    expect(chinese).toContain("本 Run 不再准入新的内部工具调用");
    expect(warningOnly).toBe("");
  });

  test("distinguishes the Lead and one Sub-Agent Task tool-call limits", () => {
    const chinese = render(
      [
        subjectBudgetObservation("lead", "a"),
        subjectBudgetObservation("subagent_task", "b"),
      ],
      "zh-CN",
    );

    expect(chinese).toContain("主 Agent 内部工具调用上限");
    expect(chinese).toContain("该子 Agent Task 的内部工具调用上限");
    expect(chinese).not.toContain("本 Run 不再准入新的内部工具调用");
  });

  test("shows the Sub-Agent total limit as a distinct progress reason", () => {
    const html = render([observation("subagent_total_limit", "f")], "en-US");

    expect(html).toContain("Sub-Agent delegation limit reached");
    expect(html).toContain("No more Sub-Agent Tasks can be admitted");
    expect(html).toContain('data-reason-code="subagent_total_limit"');
  });
});
