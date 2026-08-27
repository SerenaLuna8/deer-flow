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
  prompt: "Research the topic",
  result: "Done",
};

rs.mock("@/core/tasks/context", () => ({
  useSubtask: () => task,
  useUpdateSubtask: () => () => undefined,
}));
rs.mock("@/core/models/hooks", () => ({
  useModels: () => ({
    models: [],
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
  test("keeps a long title and status inside the header without clipping the card effects", () => {
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
    expect(html).toContain('data-subtask-context-usage="true"');
    expect(html).toContain('data-context-window-state="unavailable"');
  });
});
