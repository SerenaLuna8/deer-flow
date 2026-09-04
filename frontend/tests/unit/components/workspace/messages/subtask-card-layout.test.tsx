import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SubtaskCard } from "@/components/workspace/messages/subtask-card";
import { I18nProvider } from "@/core/i18n/context";
import type { Subtask } from "@/core/tasks/types";

const longDescription =
  "研究经典智能体理论与强化学习时代并检索 DARPA Grand Challenge 2004 2005 2007 autonomous vehicle history";

const task: Subtask = {
  id: "long-subtask",
  executionId: "44444444-4444-4444-8444-444444444444",
  status: "completed",
  statusSource: "tool_result",
  subagent_type: "general-purpose",
  description: longDescription,
  modelName: "00000000-0000-4000-8000-000000000401",
  usage: { inputTokens: 80_000, outputTokens: 7_500, totalTokens: 87_500 },
  prompt: "Research the topic",
  result: "Done",
};

rs.mock("@/core/tasks/context", () => ({
  useSubtask: () => task,
  useUpdateSubtask: () => () => undefined,
}));
rs.mock("@/core/models/hooks", () => ({
  useModels: () => ({
    models: [
      {
        name: task.modelName,
        model: task.modelName,
        display_name: "DeepSeek V4 Flash",
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: true,
        supports_vision_bridge: false,
        is_default: true,
      },
    ],
    tokenUsageEnabled: true,
  }),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: (explicit: unknown) => explicit,
  useProjectPrivateWorkScope: () => ({
    scope: { accountId: "account-1", projectId: "project-1" },
    apiBaseURL: "/api/projects/project-1/private-work",
  }),
}));

describe("SubtaskCard constrained header layout", () => {
  test("keeps the title and status while hiding model, token and context usage displays", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <SubtaskCard
          taskId={task.id}
          threadId="33333333-3333-4333-8333-333333333333"
        />
      </I18nProvider>,
    );

    const rootClass = /^<div class="([^"]+)"/.exec(html)?.[1] ?? "";
    const headerButtonClass =
      /<button[^>]*class="([^"]+)"/.exec(html)?.[1] ?? "";
    const titleClass =
      new RegExp(
        `<span[^>]*class="([^"]+)"[^>]*>${longDescription}</span>`,
      ).exec(html)?.[1] ?? "";

    expect(rootClass.split(" ")).toContain("min-w-0");
    expect(rootClass.split(" ")).not.toContain("overflow-hidden");
    expect(headerButtonClass.split(" ")).toEqual(
      expect.arrayContaining(["min-w-0", "overflow-hidden"]),
    );
    expect(titleClass.split(" ")).toEqual(
      expect.arrayContaining(["min-w-0", "truncate"]),
    );
    expect(html).not.toContain("data-subtask-context-usage");
    expect(html).not.toContain("data-context-window-state");
    expect(html).toContain("子任务已完成");
    expect(html).not.toContain("DeepSeek V4 Flash");
    expect(html).not.toContain("87.5K");
    expect(html).not.toContain("Tokens");
  });
});
