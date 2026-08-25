import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  RunActivity,
  RunDuration,
  RunExecutionActivity,
} from "@/components/workspace/messages/run-duration";
import { I18nProvider } from "@/core/i18n/context";
import {
  RUN_EXECUTION_PHASES,
  runExecutionStateSchema,
  type RunExecutionPhase,
  type RunExecutionState,
} from "@/core/threads/run-execution-state";

function executionState(
  phase: RunExecutionPhase,
  overrides: Partial<RunExecutionState> = {},
): RunExecutionState {
  return runExecutionStateSchema.parse({
    phase,
    observed_at: "2026-08-24T10:02:05Z",
    phase_started_at: null,
    execution_started_at: null,
    retry_at: null,
    run_status: phase === "terminal" ? "success" : "running",
    ...overrides,
  });
}

function render(
  state: RunExecutionState | "unavailable",
  locale: "zh-CN" | "en-US" = "zh-CN",
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <RunExecutionActivity state={state} />
    </I18nProvider>,
  );
}

describe("RunExecutionActivity", () => {
  test("renders every non-terminal authority phase in Chinese and English", () => {
    const expected = {
      queued: ["等待执行槽位", "Waiting for an execution slot"],
      waiting_for_worker: [
        "等待执行 Worker",
        "Waiting for an execution Worker",
      ],
      starting: ["Worker 已领取，正在启动", "Worker claimed; starting"],
      executing: ["执行中", "Executing"],
      retry_wait: ["等待重试", "Waiting to retry"],
      waiting_for_lease_expiry: [
        "Worker 已失联，等待租约到期",
        "Worker disconnected; waiting for lease expiry",
      ],
      waiting_for_terminalization: [
        "执行结果未知，等待安全收敛",
        "Execution outcome unknown; waiting for safe settlement",
      ],
      waiting_for_recovery: ["等待恢复执行", "Waiting to recover execution"],
      recovering: ["正在恢复执行", "Recovering execution"],
      cancelling: ["正在停止", "Stopping"],
    } as const;

    for (const phase of RUN_EXECUTION_PHASES) {
      if (phase === "terminal") continue;
      const [chinese, english] = expected[phase];
      expect(render(executionState(phase), "zh-CN")).toContain(chinese);
      expect(render(executionState(phase), "en-US")).toContain(english);
    }
  });

  test("derives reload-continuous total and phase durations only from server timestamps", () => {
    const html = render(
      executionState("executing", {
        execution_started_at: "2026-08-24T10:00:00Z",
        phase_started_at: "2026-08-24T10:02:00Z",
      }),
    );

    expect(html).toContain('data-testid="run-execution-total-duration"');
    expect(html).toContain("总执行时长 2 分 5 秒");
    expect(html).toContain('data-testid="run-execution-phase-duration"');
    expect(html).toContain("当前阶段 5 秒");
  });

  test("does not invent either duration when its authoritative timestamp is null", () => {
    const phaseOnly = render(
      executionState("waiting_for_recovery", {
        phase_started_at: "2026-08-24T10:02:00Z",
      }),
    );
    expect(phaseOnly).not.toContain("run-execution-total-duration");
    expect(phaseOnly).toContain("当前阶段 5 秒");

    const totalOnly = render(
      executionState("executing", {
        execution_started_at: "2026-08-24T10:00:00Z",
      }),
    );
    expect(totalOnly).toContain("总执行时长 2 分 5 秒");
    expect(totalOnly).not.toContain("run-execution-phase-duration");

    const neither = render(executionState("queued"));
    expect(neither).not.toContain("run-execution-total-duration");
    expect(neither).not.toContain("run-execution-phase-duration");
    expect(neither).not.toContain("不足 1 秒");
  });

  test("shows unavailable without an activity shimmer and fails closed on invalid input", () => {
    for (const [locale, label] of [
      ["zh-CN", "执行状态暂不可用"],
      ["en-US", "Execution status temporarily unavailable"],
    ] as const) {
      const html = render("unavailable", locale);
      expect(html).toContain(label);
      expect(html).not.toContain("run-execution-phase-shimmer");
    }

    const invalid = {
      ...executionState("executing"),
      worker_id: "must-not-leak",
    } as never;
    expect(render(invalid)).toContain("执行状态暂不可用");
  });

  test("renders no activity or shimmer for a terminal projection", () => {
    const html = render(executionState("terminal"));

    expect(html).toBe("");
    expect(html).not.toContain("run-execution-phase-shimmer");
  });

  test("keeps the existing RunActivity and historical RunDuration contracts", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <RunActivity startTime={null} />
        <RunDuration durationSeconds={3} />
      </I18nProvider>,
    );

    expect(html).toContain('data-testid="run-activity"');
    expect(html).toContain("执行中…");
    expect(html).toContain('data-testid="run-duration"');
    expect(html).toContain("本次任务耗时 3 秒");
  });
});
