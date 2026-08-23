import { describe, expect, test } from "@rstest/core";

import {
  emptyRunControlProgress,
  mergeRunControlObservations,
  parseRepeatedCallEvent,
  parseRunControlEventRows,
  parseRunControlLiveEvent,
  parseSubagentLimitEvent,
  parseToolCallBudgetEvent,
} from "@/core/threads/tool-call-control-events";

const RUN_ID = "50000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";

function repeatedCallEvent(overrides: Record<string, unknown> = {}) {
  return {
    type: "repeated_call",
    schema_version: 1,
    reason_code: "repeated_call_limit",
    workload_profile: "research",
    role: "lead",
    run_id: RUN_ID,
    execution_id: null,
    count_before: 3,
    proposed: 1,
    admitted: 0,
    rejected: 1,
    count_after: 4,
    warn_threshold: 2,
    hard_limit: 4,
    disposition: "tool_free_finalization",
    observation_id: "a".repeat(64),
    ...overrides,
  };
}

function toolBudgetEvent(overrides: Record<string, unknown> = {}) {
  return {
    type: "tool_call_budget",
    schema_version: 2,
    reason_code: "tool_budget_exhausted",
    workload_profile: "research",
    role: "lead",
    run_id: RUN_ID,
    execution_id: null,
    count_before: 199,
    proposed: 3,
    admitted: 1,
    rejected: 2,
    count_after: 200,
    hard_limit: 200,
    disposition: "truncate_tool_calls",
    observation_id: "b".repeat(64),
    ...overrides,
  };
}

function legacyToolBudgetEvent(overrides: Record<string, unknown> = {}) {
  return {
    ...toolBudgetEvent(),
    schema_version: 1,
    tool_name: "web_search",
    count_before: 9,
    count_after: 10,
    warn_threshold: 6,
    hard_limit: 10,
    disposition: "exhaust_tool",
    ...overrides,
  };
}

function subagentLimitEvent(overrides: Record<string, unknown> = {}) {
  return {
    type: "subagent_limit",
    schema_version: 1,
    reason_code: "subagent_total_limit",
    role: "lead",
    run_id: RUN_ID,
    count_before: 8,
    proposed: 3,
    admitted: 1,
    rejected: 2,
    count_after: 9,
    hard_limit: 9,
    disposition: "truncate_tool_calls",
    observation_id: "c".repeat(64),
    ...overrides,
  };
}

function durableRow(
  eventType: string,
  content: Record<string, unknown>,
  reasonCode: string,
  observationId: string,
  seq: string,
) {
  return {
    thread_id: THREAD_ID,
    run_id: RUN_ID,
    event_type: eventType,
    category: "middleware",
    content,
    metadata: {
      reason_code: reasonCode,
      observation_id: observationId,
    },
    seq,
    created_at: "2026-08-23T00:00:00Z",
  };
}

describe("Run-control event contracts", () => {
  test("accepts independent strict repeated-call and tool-budget live frames", () => {
    expect(parseRepeatedCallEvent(repeatedCallEvent())).toEqual(
      repeatedCallEvent(),
    );
    expect(parseToolCallBudgetEvent(toolBudgetEvent())).toEqual(
      toolBudgetEvent(),
    );
    expect(parseToolCallBudgetEvent(repeatedCallEvent())).toBeNull();
    expect(parseRepeatedCallEvent(toolBudgetEvent())).toBeNull();

    expect(
      parseRepeatedCallEvent(
        repeatedCallEvent({ tool_name: "must-not-cross-contracts" }),
      ),
    ).toBeNull();
    expect(
      parseToolCallBudgetEvent(toolBudgetEvent({ tool_name: "web_search" })),
    ).toBeNull();
  });

  test("normalizes the historical tool_call_control live frame into the new split contracts", () => {
    const repeated = parseRunControlLiveEvent({
      ...repeatedCallEvent(),
      type: "tool_call_control",
      tool_name: null,
    });
    expect(repeated).toEqual(repeatedCallEvent());

    const budget = parseRunControlLiveEvent({
      ...legacyToolBudgetEvent(),
      type: "tool_call_control",
    });
    expect(budget).toEqual(legacyToolBudgetEvent());
  });

  test("keeps the Sub-Agent total limit on its independent strict live contract", () => {
    const event = subagentLimitEvent();
    expect(parseSubagentLimitEvent(event)).toEqual(event);
    expect(parseRepeatedCallEvent(event)).toBeNull();
    expect(parseToolCallBudgetEvent(event)).toBeNull();
    expect(
      parseSubagentLimitEvent({
        ...event,
        task_args: { prompt: "private" },
      }),
    ).toBeNull();
  });

  test("rejects unknown/high-cardinality fields and inconsistent counters", () => {
    expect(
      parseToolCallBudgetEvent(
        toolBudgetEvent({ query: "private query must not enter UI state" }),
      ),
    ).toBeNull();
    expect(
      parseToolCallBudgetEvent(
        toolBudgetEvent({ admitted: 2, rejected: 2, proposed: 3 }),
      ),
    ).toBeNull();
    expect(
      parseRepeatedCallEvent(
        repeatedCallEvent({ count_after: 3, observation_id: "not-a-digest" }),
      ),
    ).toBeNull();
  });

  test("deduplicates live plus replay by observation_id and resets for a new Run", () => {
    const first = parseToolCallBudgetEvent(toolBudgetEvent());
    expect(first).not.toBeNull();
    if (!first) return;

    const once = mergeRunControlObservations(emptyRunControlProgress(), [
      first,
    ]);
    const replayed = mergeRunControlObservations(once, [first]);
    expect(replayed.observations).toHaveLength(1);

    const nextRun = parseToolCallBudgetEvent(
      toolBudgetEvent({
        run_id: "50000000-0000-4000-8000-000000000002",
        observation_id: "d".repeat(64),
      }),
    );
    expect(nextRun).not.toBeNull();
    if (!nextRun) return;
    expect(mergeRunControlObservations(replayed, [nextRun])).toEqual({
      runId: nextRun.run_id,
      observations: [nextRun],
    });
  });

  test("normalizes the three current durable event types and historical rows", () => {
    const { type: _repeatedType, ...repeatedPayload } = repeatedCallEvent();
    const { type: _budgetType, ...budgetPayload } = toolBudgetEvent();
    const { type: _subagentType, ...subagentPayload } = subagentLimitEvent();
    expect([_repeatedType, _budgetType, _subagentType]).toEqual([
      "repeated_call",
      "tool_call_budget",
      "subagent_limit",
    ]);

    const rows = parseRunControlEventRows([
      durableRow(
        "middleware:repeated_call",
        repeatedPayload,
        "repeated_call_limit",
        "a".repeat(64),
        "10",
      ),
      durableRow(
        "middleware:tool_call_budget",
        budgetPayload,
        "tool_budget_exhausted",
        "b".repeat(64),
        "11",
      ),
      durableRow(
        "middleware:subagent_limit",
        subagentPayload,
        "subagent_total_limit",
        "c".repeat(64),
        "12",
      ),
      {
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        event_type: "middleware:subagent_limit",
        category: "middleware",
        content: {
          name: "SubagentLimitMiddleware",
          hook: "after_model",
          action: "truncate_tool_calls",
          changes: {
            reason: "subagent_total_limit",
            max_total: 9,
            prior_delegations: 8,
            admitted_task_calls: 1,
            dropped_task_calls: 2,
          },
        },
        metadata: {},
        seq: "13",
        created_at: "2026-08-23T00:00:01Z",
      },
    ]);

    expect(rows.map((row) => row.type)).toEqual([
      "repeated_call",
      "tool_call_budget",
      "subagent_limit",
      "subagent_limit",
    ]);
    expect(rows.at(-1)).toMatchObject({
      reason_code: "subagent_total_limit",
      run_id: RUN_ID,
      proposed: 3,
      admitted: 1,
      rejected: 2,
      hard_limit: 9,
    });
  });

  test("normalizes a legacy repeated-call durable row and rejects mismatched metadata", () => {
    const { type: _type, ...legacyPayload } = {
      ...repeatedCallEvent(),
      type: "tool_call_control",
      tool_name: null,
    };
    expect(_type).toBe("tool_call_control");
    expect(
      parseRunControlEventRows([
        durableRow(
          "middleware:tool_call_budget",
          legacyPayload,
          "repeated_call_limit",
          "a".repeat(64),
          "14",
        ),
      ]),
    ).toEqual([repeatedCallEvent()]);

    expect(() =>
      parseRunControlEventRows([
        durableRow(
          "middleware:repeated_call",
          legacyPayload,
          "tool_budget_exhausted",
          "a".repeat(64),
          "15",
        ),
      ]),
    ).toThrow();
  });
});
