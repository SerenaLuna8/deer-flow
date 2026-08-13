import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillBuilderRunActivity } from "@/components/projects/skills/skill-builder-run-activity";

const RUN_ID = "44444444-4444-4444-8444-444444444444";

describe("SkillBuilderRunActivity", () => {
  test("shows an explicit empty state before tool events are available", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderRunActivity
        activeRun={{
          runId: RUN_ID,
          status: "running",
          streamUrl: "/api/stream",
        }}
      />,
    );

    expect(html).toContain("正在执行");
    expect(html).toContain("尚无工具步骤");
    expect(html).toContain("当前后端仅提供了可靠的 Run 状态");
  });

  test("renders only projected tool names and statuses", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderRunActivity
        projection={{
          runId: RUN_ID,
          status: "running",
          messages: [],
          toolSteps: [
            {
              id: "step-1",
              toolName: "project_asset_catalog",
              status: "completed",
            },
          ],
          clarification: null,
        }}
      />,
    );

    expect(html).toContain("project_asset_catalog");
    expect(html).toContain("已完成");
  });

  test("keeps the terminal outcome visible after activeRun is removed", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderRunActivity
        presentation={{ runId: RUN_ID, status: "cancelled" }}
      />,
    );

    expect(html).toContain("本轮执行已取消");
  });

  test("explains how to resume after the model output limit", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderRunActivity
        presentation={{ runId: RUN_ID, status: "error" }}
        failureCode="MODEL_OUTPUT_LIMIT"
      />,
    );

    expect(html).toContain("本轮达到模型输出上限");
    expect(html).toContain("已成功写入的候选草稿仍然保留");
    expect(html).toContain("基于现有草稿继续完成");
  });
});
