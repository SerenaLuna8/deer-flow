import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SubtaskCard } from "@/components/workspace/messages/subtask-card";
import { I18nProvider } from "@/core/i18n/context";
import type { Subtask } from "@/core/tasks/types";

let currentTask: Subtask;

rs.mock("@/core/tasks/context", () => ({
  useSubtask: () => currentTask,
  useUpdateSubtask: () => () => undefined,
}));
rs.mock("@/core/models/hooks", () => ({
  useModels: () => ({ models: [], tokenUsageEnabled: false }),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: (explicit: unknown) => explicit,
  useProjectPrivateWorkScope: () => ({
    scope: { accountId: "account-1", projectId: "project-1" },
    apiBaseURL: "/api/projects/project-1/private-work",
  }),
}));

const baseTask: Subtask = {
  id: "parent-task-call",
  status: "in_progress",
  statusSource: "execution_approval",
  subagent_type: "general-purpose",
  description: "Execute a delegated command",
  prompt: "Use Bash",
};

function render(task: Subtask) {
  currentTask = task;
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <SubtaskCard taskId={task.id} />
    </I18nProvider>,
  );
}

describe("SubtaskCard delegated execution approval", () => {
  test("renders pending approval as paused instead of running", () => {
    const html = render({
      ...baseTask,
      executionApproval: {
        approvalId: "11111111-1111-4111-8111-111111111111",
        status: "pending",
      },
    });

    expect(html).toContain("等待审批");
    expect(html).not.toContain("子任务运行中");
    expect(html).not.toContain("animate-spin");
    expect(html).not.toContain("animate-shine");
  });

  test("renders a denied approval as a settled failure", () => {
    const html = render({
      ...baseTask,
      status: "failed",
      executionApproval: {
        approvalId: "11111111-1111-4111-8111-111111111111",
        status: "denied",
      },
    });

    expect(html).toContain("已拒绝");
    expect(html).not.toContain("子任务运行中");
    expect(html).not.toContain("animate-spin");
  });

  test("keeps a completed tool-budget-capped task successful while showing the cap", () => {
    const html = render({
      ...baseTask,
      status: "completed",
      statusSource: "tool_result",
      stopReason: "tool_budget_capped",
      result: "Useful partial research",
      executionApproval: undefined,
    });

    expect(html).toContain('data-subtask-stop-reason="tool_budget_capped"');
    expect(html).toContain("子任务已达到工具调用额度");
    expect(html).toContain("子任务已完成");
    expect(html).not.toContain("子任务失败");
  });

  test("shows Provider truncation beside the preserved partial result", () => {
    const html = render({
      ...baseTask,
      status: "completed",
      statusSource: "tool_result",
      stopReason: "output_truncated",
      result: "Useful partial research",
      executionApproval: undefined,
    });

    expect(html).toContain('data-subtask-stop-reason="output_truncated"');
    expect(html).toContain("Provider 截断了子任务输出");
    expect(html).toContain("子任务已完成");
    expect(html).not.toContain("子任务失败");
  });
});
